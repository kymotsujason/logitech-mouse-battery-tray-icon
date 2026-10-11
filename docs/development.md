# Development

This page covers building Mouse Battery from source, how the code is laid out, how a release is made, and what hasn't been tested yet.

## Build from source

The app is plain Python and needs Python 3.11 or later with PyQt6 on Qt 6.4 or later. The tests run with:

```bash
python3 -m unittest discover -s tests -t .
```

`packaging/build.sh` builds the `.deb` and `.rpm` into `dist/` with nfpm's Docker image. `packaging/tarball.sh` builds the release tarball from the last commit, and `packaging/buildArch.sh` builds the Arch package from it. `tests/containers/testPackages.sh` installs each package in a clean container, checks it, and removes it again.

To install from a clone instead of a release, build the package for your distro with Docker running, then install the file it leaves in `dist/`. On Arch that's:

```bash
packaging/tarball.sh
packaging/buildArch.sh
sudo pacman -U dist/logitech-mouse-battery-<VERSION>-1-any.pkg.tar.zst
```

Replace `<VERSION>` with the version in `src/version.py`.

On Debian, Ubuntu, or Fedora, run `packaging/build.sh` and install the `.deb` from `dist/` with `sudo apt install` or the `.rpm` with `sudo dnf install`, giving the path with a leading `./`.

## Make a release

Run `python3 packaging/bumpVersion.py <VERSION>`, where `<VERSION>` is the new version number. It updates every file that carries the version, and the tests fail if any of them disagree. Add the release's section to `CHANGELOG.md`, since the release notes come from it. Then commit and push to `main`, and the release workflow builds, tests, and publishes `v<VERSION>` with the packages attached, since that version has no release yet. A push that keeps the version publishes nothing.

## Code layout

The app is the flat set of modules in `src/`, which the packages install side by side in `/usr/share/logitech-mouse-battery/`:

- `tray.py` starts the app and owns the tray icon, its menu and tooltip, Open at Login, and About.
- `service.py` is the battery service, the only part that opens receivers and cables. It serves their readings and the saved history on the system bus, and it runs its own UPower watcher so the history covers the mice UPower reads too.
- `receivers.py` finds, opens, and reads the receivers and cables in the service, with a worker thread for each HID++ node.
- `serviceState.py` is the state the service sends and the tray reads.
- `serviceClient.py` follows the service from the tray, sends it read requests, and asks it for the history and chart levels the window shows.
- `clearAccess.py` closes the access 1.0.x gave plugged in receivers and cables, when the package is installed.
- `exceptionHooks.py` prints an exception in a slot or a thread instead of ending the app or the service.
- `trayHost.py` waits for a system tray and sends a notice when none shows up.
- `installWatch.py` quits the app when it's uninstalled and restarts it into a new version after an update.
- `mice.py` keeps the tray's list of mice, merging HID++ and UPower readings by mouse, with the status and the timed read.
- `history.py` saves each mouse's readings, charges, discharges, and steps in the service, and puts together the history the window shows.
- `health.py` turns a history into the window's health line and figures.
- `historyWindow.py` is the battery window, with `glass.py` for its colors and glass panels and `charts.py` for its charts.
- `settings.py` saves the window's sleep check setting and chart range.
- `hidpp.py` speaks Logitech's HID++ protocol over hidraw.
- `hidDescriptor.py` checks a HID report descriptor, for discovery and for the udev rule.
- `isHidpp.py` is the check the udev rule runs.
- `upower.py` reads the mice the kernel drives through UPower, for the tray and the window and for the service's history.
- `lowBattery.py` decides when a mouse gets a low battery warning.
- `notify.py` sends desktop notifications.
- `dbusCalls.py` makes the tray's D-Bus calls to the desktop and to UPower with a timeout, answering through a callback so a slow peer never holds it still. It also makes the blocking calls, each with a timeout, for the terminal check (to the battery service and UPower) and for the notice a second tray sends before it quits, and it unwraps the values in a reply. `serviceClient.py` makes the tray's own state and read calls to the battery service with its own callbacks and timeout, while its history and chart level calls go through `dbusCalls.py`.
- `panelColor.py` works out the color the icon is drawn in from the desktop's theme, and reads the desktop portal's color scheme and reduced motion settings, which the window follows too.
- `icon.py` draws the tray icon and the mouse picture in the battery window, and picks the window's title bar icon.
- `autostart.py` turns Open at Login on and off.
- `check.py` is the terminal check.
- `version.py` holds the version.

`diagnostics/` holds scripts for testers to run, which aren't packaged. `diagnoseMouse.sh` saves what the kernel, udev, UPower, and the app see of a mouse on its receiver and on its cable, and `watchSleep.sh` logs the kernel battery, its uevents, and UPower while the mouse sleeps and wakes.

`packaging/` holds the launcher, the desktop entries, the udev rule, the service's systemd, D-Bus, and sysusers files, the install scripts, the package configs, and the build scripts. `tests/` holds the unit tests, with the fake devices and D-Bus services in `tests/fakes/`, the checks that run the real tray on private buses in `tests/live/`, the container checks in `tests/containers/`, and the checks that run the packaged service on a booted runner in `tests/booted/`.

## Not tested

These haven't been tested yet:

- The GNOME, GNOME Classic, GNOME Flashback, XFCE, Cinnamon, MATE, Budgie, and LXQt desktops
- Mice other than the PRO X3 SUPERSTRIKE, the PRO X Wireless, and the MX Master 3S
- Any Logitech mouse through UPower
- Bluetooth mice
- Light app themes on panels that stay dark outside Plasma and GNOME
- Double click installs in Ubuntu's App Center and GNOME Software
- An Open at Login override that another tool wrote with GNOME's `X-GNOME-Autostart-enabled` key
- The `.deb` and `.rpm` on a real Debian, Ubuntu, or Fedora desktop, including the rule applying without a replug (only clean containers have run them, plus the `.deb` on GitHub's Ubuntu runner with a fake receiver)
- The service on a booted Debian, Fedora, or Arch system before release, since only GitHub's Ubuntu runner and clean containers run it automatically
