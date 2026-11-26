# Network: Wi-Fi setup, names and the hub

How a Matrix gets onto Wi-Fi, stays on it, and is found. The design is the accepted
"Wi-Fi setup and hub proposal" (12 Nov 2026); the code is `bongo/net.py`, `bongo/netname.py`,
`bongo/setup_screen.py` and `bongo/hub.py`.

## What happens

| When | What the Matrix does |
|---|---|
| Starts with no saved Wi-Fi | Opens setup straight away. |
| Starts with saved Wi-Fi | Joins it (NetworkManager), nudging the strongest saved network every minute while it can't. |
| Starts with saved Wi-Fi that isn't there, and no browser has ever paired | Opens setup after 90 seconds, not 5 minutes: it is being set up, and that network (one from the image, a typo) isn't coming. Setup still closes by itself if the network appears. |
| Loses its Wi-Fi | Keeps trying for 5 minutes, then opens setup. Screens keep playing throughout. |
| Setup is open | The panel shows a Wi-Fi join code, the setup network's name and password, and `SETUP PAGE 10.42.0.1`. Every 2 minutes it looks for a saved network and leaves setup if one is back. After 15 minutes with nobody using setup, it closes setup and tries saved networks again. |
| A phone joins `Matrix-Setup-XXXX` | The setup page opens by itself (every name looked up on that network answers with the Matrix). If it doesn't, open `10.42.0.1`. |
| You choose a network on the setup page | The Matrix leaves setup to try it (it has one radio, so the phone drops off for a moment). If it works, it's kept and the panel shows the new address and a code for the dashboard. If not, nothing is kept, setup comes back with the same password, and the page says why. |
| A cable is plugged in | Counts as connected: setup never opens. |
| Its name or address changes | The panel shows the name, address and a dashboard code for 10 s. |

- **Names:** the display name becomes the network name: `Kitchen` is `kitchen.local`. Until it's named it is `matrix-7f3a` (the end of its hardware address).
- **Port 80:** `http://kitchen.local` works with no `:5000` (turn off with `"web": {"port80": false}`).
- **Setup password:** a new one each time setup opens (`kite-4729`), so one seen on a panel isn't good forever. It stays the same while one setup is open, including after a failed try and a "Look again".
- **Setup network's security:** plain WPA2 (AES), with protected management frames off. NetworkManager's own hotspot defaults make many phones answer "incorrect password".
- **Saved networks** are matched by network name, whatever their connection is called (Raspberry Pi Imager's is `preconfigured`).
- **Joining from the setup page:** the Matrix waits until it can see the network again (the radio was the hotspot a moment before), and tries once more, spelled out, if NetworkManager says it can't find it. A hidden network is joined from a profile that names its security.
- **Radio:** switched on (`rfkill unblock wifi`, `nmcli radio wifi on`) when the display starts, whatever left it off.
- **Wi-Fi country:** `wifi_country` in the config (default `GB`; the setup page has a list). Raspberry Pi OS keeps Wi-Fi switched off until a country is set.
- **Wi-Fi power saving:** turned off at start (`/etc/NetworkManager/conf.d/bongo-wifi-awake.conf`, and `iw` for the network it's on). With it on, the chip misses the broadcasts phones use to find the Matrix, so a phone loses it while a PC that already knew its address doesn't.
- **One radio:** the check every 5 s only reads what NetworkManager already knows; it never makes the radio look around (a Pi Zero's one radio drops off its network for a moment while it does). Scans happen only while joining or in setup.
- **Reopen setup:** Settings > Wi-Fi, for a new router or password. Saved networks stay saved.

## The hub

Every Matrix can be the hub; the one that has been running longest is (lowest id breaks a tie),
and it answers at `http://matrix.local`. The page lists every Matrix on the network with its live
picture, what's playing, its version, health, and an **Open dashboard** link. It is read-only: each
Matrix still pairs on its own. Anyone on the same Wi-Fi can see it, so it shows names and what's
playing, never settings, feeds or messages. While someone previews an unsaved draft on the panel from
the screen editor, the hub's picture stays the playlist's (a draft can hold private text). If the hub
is unplugged, the next one takes over within about a minute.

### Updates from the hub

Each card says which build its Matrix runs (the short commit Settings > Software shows) and
"up to date" or "update available". A card with an update has an **Update…** button.

- **The hub can't update anything.** It is another site to every Matrix, and each one pairs with a
  browser on its own, so there is no "Update all". Update… is only a link to that Matrix's own
  dashboard, `http://<it>/settings#update`: Settings opens at the Software card with
  "<Name> is on <version> (<build>). A new version (<new build>) is ready." and **Update now** / **Not now**. Update now does
  what Pull update does (`bongo/update.py`: if the new version won't start, the old one comes back by
  itself). Not now leaves the usual card.
- **Pairing first.** A browser that hasn't paired with that Matrix gets its pairing page, then lands
  on the Software card with the question (not on Now). The link carries no secret: `#update` only
  says where to go, and never reaches the Matrix.
- **What the hub knows.** Each Matrix adds four things to its public card (`/api/public`): `label`
  (its version as people are shown it, `v1.0.0`, or `v1.0.0.<branch>` on a branch other than `main`, the
  branch made safe to show), `build` (the short commit it runs), `date` (the day that commit was made, a
  `YYYY-MM-DD` string; the hub page shows it beside the label) and `update` (true or false), or `updating` while an update is under
  way, and `rolled_back` (true) when the update on offer was already tried and wouldn't start: the
  card then says "Last try rolled back: it didn't start" by the button, which still works. Never the
  update source's address or the new version (the label carries the update branch's name, nothing else). `update` is what the Matrix already knows from its
  own look for a new version (five minutes after it starts, then once a day, or when you press Check
  for updates): the hub asking never makes it look. A look that fails (no network yet) is tried again
  after 15 minutes, an hour, then every 6 hours until one works. Until its first look it says nothing,
  and the card shows just the build.
- **If Update now can't start** (the Matrix is restarting, something went wrong), the question stays
  with the reason under the buttons. If another phone or tab had already started the update, the
  card shows its progress instead.
- **A sign-in in the update address is never logged, shown or written again.** A failed look or
  update says why in plain words on the dashboard and in the status file ("the update source's name
  wasn't found", "its certificate isn't trusted", "answered with an error (500)", "another git job left
  a lock behind", "the SD card is read-only"). Logs also has git's own last lines for diagnosis, with
  any sign-in removed and the address, its host and the branch replaced by "the update source" and
  "the update branch". While an update runs, the status file says "Fetching <branch> from <address>"
  with the sign-in (`name:token@`) stripped from the address. If git can't put the old version back
  either, the update ends as "failed" and says so, rather than stopping half-way.
- **Older builds** send none of this: their card shows "older build" when they are 3.x (they have no label, and the 3.x number is not shown); a build that announces a public-style number without a label shows that number, and the
  "update available" badge when another Matrix announces a newer version number) and has no button.
  Nor has a Matrix that isn't answering. The button also needs the hub Matrix itself to be on a
  build with this: the page comes from the hub.

`zeroconf` (pure Python, in requirements.txt) does the mDNS; the updater installs it.

## Testing on the Pi

CI covers the logic with a fake NetworkManager, a fake clock and fake peers. These need real hardware:

1. **Names:** name the Matrix in Settings, then open `http://<name>.local` from a phone and a PC
   (some Android phones and work laptops can't; the panel's address screen is the fallback).
   Rename it and check the new name works within a minute, with no reboot (avahi is told directly;
   on Windows run `ipconfig /flushdns` first, as it remembers a failed lookup for a few minutes).
2. **Router unplugged:** unplug the router for under 5 minutes: the Matrix rejoins by itself with no
   setup screen. Unplug it for longer: setup opens after 5 minutes, and closes again within
   2 minutes of the router coming back.
3. **Power cut:** pull the power to the Matrix and the router together and power both up; the Matrix
   must not get stuck in setup while the router boots.
4. **Setup from a phone:** Settings > Wi-Fi > Reopen setup, then join from an iPhone and an Android
   phone: by the join code, and by typing the name and password. Check the setup page pops up by
   itself. Try a wrong password (setup comes back and says why), then the right one.
5. **Pi Zero 2.4 GHz:** with a 5 GHz-only network, check the setup page's 2.4 GHz tip shows.
6. **Hub:** with one Matrix, `http://matrix.local` shows its own card. With two on one network,
   check both cards, then unplug the hub (the one running longest) and check the other takes over
   within about a minute.
7. **Update from the hub:** with a new commit on the update branch, wait for the daily look (or press
   Check for updates on that Matrix): its hub card says "update available". Press Update… from a
   browser that has never paired with it: pair, and check the Software card asks, Update now updates,
   and the hub card then says "updating…" and afterwards "up to date".
