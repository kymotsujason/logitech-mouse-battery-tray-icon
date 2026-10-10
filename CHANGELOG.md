# Changelog

This file lists the changes in each release of Mouse Battery, newest first.

## 1.2.0, unreleased

### Added

- A battery window opens when you click the tray icon or pick "Battery Health" from its menu, with the mouse, its level, and a sidebar when you have more than one mouse, and it fits small screens, works from the keyboard and a screen reader, comes back when you click the tray icon while it's minimized, and moves smoothly when you switch charts, ranges, or mice or open its sleep menu.
- The window shows a level chart for the last 24 hours, 7 days, 30 days, 6 months, a year, or all time, and it remembers the range you picked.
- The window shows battery health, including how many hours a charge lasts, how fast it charges month by month, and the number of charge cycles.
- The health box shows how many hours of use a full charge gives and how long charging takes, counts down the charges left until the wear check, and warns when a charge lasts 15% less, or charging takes 15% longer, than it used to.
- The battery service now saves each mouse's charges and discharges, so the history keeps growing while the window is closed, and it survives a crash, a power loss, a clock change, a service restart, and a mouse moving between UPower and direct reading, while a damaged history file is moved aside and started fresh. Removing the package leaves the history in place, and the README says how to delete it.
- Any local account can read the saved history through the service, with its own read limits, as the README says.
- The history takes in only so many new mice at a time, so a flood of new devices can't fill it.
- A "Check whether mice are asleep" setting in the window reads every mouse every 30 seconds, every minute, or every 5 minutes, so the icon turns gray soon after a mouse sleeps.
- The window's title bar icon follows the shown mouse, white in use, green while charging, red when low, and gray while asleep.
- On KDE, the window blurs the desktop behind it when KWin supports it.
- The window opens again by itself after an update restarts the app.
- The window's glow is blue while a mouse is in use and green while it charges, so the two are easy to tell apart.

### Changed

- The number in the tray icon is drawn in blocky digits that the fill line never crosses.
- The app icon in menus and the About box is now a neutral mouse that reads on light and dark themes.
- Mice that don't report their battery on their own are now read every 5 minutes, and a mouse that does report on its own is checked only when you open the menu or the window, or when you turn on the sleep check.

### Fixed

- A mouse that reports its battery as a rough level or a voltage now gets an estimated level for its icon and its warnings.
- A mouse whose serial number shows up in UPower after its first reading stays one mouse instead of two, and an older UPower reading no longer replaces a newer one from the battery service.
- When the notification service is swapped or restarted, the app asks the new one again how to format a notice, and a click on another notice can't set off an old notice's action.

## 1.1.1, 2026-10-08

### Added

- Each release file now comes with a signed record of the commit and workflow run that built it, which you can check with `gh attestation verify`.
- The tray and `check` now say when the battery service is gone or is turning your reads down, and show the last reading as possibly old.

### Changed

- The tray starts the battery service again when you open the menu or click the icon, and tries on its own once an hour.
- Low battery notices are limited to one per mouse each hour and four in total each hour, so a flapping mouse can't flood you.
- The service stops cleanly when the system stops it, and its restarts are spaced out and capped.
- The terminal check can now be ended with Ctrl+C.

### Fixed

- Installing on Arch no longer waits 30 seconds and fails at the udev step.
- The tray no longer freezes for a moment when UPower, the desktop, the notification service, or the tray host is slow to answer.
- Checking a new version after an update no longer holds the tray up.
- The About box opens again each time on KDE, and it comes back after you close it from the title bar.
- A receiver that was denied access is shown in the menu next to your mice, and same named mice keep their numbers when one leaves and comes back.
- "Open at Login" now says why it didn't change when it can't.
- A mouse is read again after an error, a refused write, or a pairing that was missed, so it recovers faster.
- Device names with angle brackets no longer break the tooltip.
- Charging states reported as voltage now match what the Linux kernel shows.

### Security

- The service limits how much work a device or a local account can make it do, including how many reads each account can start.
- Open at Login no longer follows links into another account's folders, so a run as root can't be led into system files.
- A device's answers are now checked for the right shape and size before they're used.
- The install no longer removes a seat's access from the wrong device or too early, and it keeps access that another rule such as Solaar's gives.
- Only a person's read can start a stopped service, so a timer can't keep restarting one that is failing.

## 1.1.0, 2026-10-02

### Added

- A battery service now reads receivers and cables, running as its own system user in a systemd sandbox, and it serves battery readings on the system bus.
- The tray follows the service, keeps its mice through a service restart, and `check` asks the service for readings and says when it can't reach it.

### Changed

- The udev rule now hands receivers and cables to the service's group instead of giving access to the logged in user.
- The README and package data now describe what the service's access covers, and that a program that already had a receiver open keeps it until it closes or you replug.

### Fixed

- A fresh install no longer reports a failure from a service that nothing has started yet.
- The tray keeps UPower's reading when UPower goes away, until the service gives a later one.

### Security

- Programs you run can no longer open your receiver or cable and send it HID++ commands, since only the service can. This closes the access that 1.0.0 and 1.0.1 gave, including the chance of reaching a keyboard paired to the same receiver.
- Installing removes the access that earlier versions gave to receivers that are already plugged in.

## 1.0.1, 2026-10-01

### Added

- More Logitech receivers are covered, including the LIGHTSPEED `046d:c547` and the Logi Bolt `046d:c548`, and the README now lists the mice a tester tried on them.

### Changed

- The README now says plainly what the udev rule allows, including that a program you run could send HID++ commands to a keyboard paired to the same receiver.
- The README now says that a turned off mouse keeps showing as awake until the app reads it again.
- Release files now include a `SHA256SUMS` file.

### Fixed

- A low battery warning now arms again after the mouse charges above 20%, even when the app didn't see the charge, and a warning is sent again after a login or an update while the mouse is still low.
- A warning that couldn't be delivered is tried again on the next reading, and a mouse that reconnects isn't warned again each time.
- Mice read through UPower are kept through failed or slow UPower calls and retried, instead of vanishing from the menu.
- The tray keeps looking for a system tray, and the no tray notice and its timers now stop properly when the app quits.
- Open at Login now follows GNOME's `X-GNOME-Autostart-enabled` key, and it won't rewrite an override that another user owns.
- The first notification now works on Qt 6.4, and notices are escaped only for servers that accept markup.
- Mouse lines now name a charging error, a pending charge, and an unknown state, and a reading from an earlier day shows its date.
- The About box opens once and comes forward on a second click, and Ctrl+C now ends the app.
- Reading a mouse is more reliable. Short or bad HID++ reports are dropped, busy and unreachable devices are told apart, a mouse that keeps failing stops being read, and failed searches and denied retries back off.
- An unknown launcher argument now prints a usage line instead of starting.

### Security

- The packages install the app files with safer permissions, and the release workflow gets only the access it needs and publishes only from main.
- Device names have control characters removed before they're shown.

## 1.0.0, 2026-09-28

### Added

- A tray icon shows the battery level of each connected Logitech mouse, filling up with the level and turning green while charging, red at 20% or below, and gray while asleep.
- The menu lists every mouse with its level, charging state, and the time of its last reading when it's asleep, and the app follows the mouse you're using.
- A warning arrives once at 10% and once more at 5% on each charge.
- Mice the kernel already drives are read through UPower, and mice on receivers or cables the kernel leaves alone, such as the PRO X3 SUPERSTRIKE, are read over Logitech's HID++ protocol.
- A udev rule gives access only to HID++ interfaces that pass the app's own check, and it applies to devices that are already plugged in.
- The app can open at login for every account, and an account can turn that off from the menu.
- On GNOME, a notification offers to turn on the AppIndicator extension when there's no tray.
- `logitech-mouse-battery check` prints each mouse it can read with its battery in a terminal.
- Packages are available for Debian and Ubuntu (`.deb`), Fedora (`.rpm`), and Arch (`.pkg.tar.zst`).
