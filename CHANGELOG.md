# Changelog

This file lists the changes in each release of Mouse Battery, newest first.

## 1.2.0, 2026-10-11

### Added

- A battery window opens when you click the tray icon or pick "Battery Health" from its menu. It shows each mouse's level, a level chart for the last day, week, or month, how long a charge lasts and how fast it charges month by month, and the number of charge cycles.
- A health check says when a charge lasts 15% less, or charging takes 15% longer, than when tracking began. Once it has measured enough, it also says how many hours a full charge gives and how long charging takes.
- The battery service keeps each mouse's charge history, even while the window is closed. Any account on the computer can read it, and removing the package leaves it in place.
- An optional "Check whether mice are asleep" setting turns the icon gray soon after a mouse sleeps.
- The window works from the keyboard and with a screen reader.

### Changed

- The number in the tray icon uses blocky digits that stay readable.
- The app icon is now a neutral mouse that suits light and dark themes.
- A mouse that reports its own battery is no longer read on a timer, while other mice are read every 5 minutes.

### Fixed

- A mouse that reports only a rough level or a voltage now gets an estimated level.
- A mouse seen through both UPower and the battery service no longer shows up twice.
- Notices keep working after the notification service restarts.

## 1.1.1, 2026-10-08

### Added

- Each release file comes with a signed record of how it was built, which `gh attestation verify` checks.
- The tray and `check` say when the battery service is gone or is turning your reads down.

### Changed

- The tray starts the battery service again when you open the menu, and tries on its own once an hour.
- Low battery notices are limited, so a flapping mouse can't flood you.
- The service stops cleanly, and its restarts are spaced out.

### Fixed

- Installing on Arch no longer waits 30 seconds and fails at the udev step.
- The tray no longer freezes when UPower, the desktop, or the notification service is slow to answer.
- The About box opens every time on KDE.
- "Open at Login" says why it didn't change when it can't.
- A receiver that was denied access shows in the menu, and a mouse recovers faster after a read error.
- Device names with angle brackets no longer break the tooltip.

### Security

- The service limits how much work a device or a local account can make it do.
- Open at Login no longer follows links into another account's folders.
- A device's answers are checked before they're used.
- Installing keeps the access that another rule, such as Solaar's, gives.

## 1.1.0, 2026-10-02

### Added

- A battery service now reads receivers and cables, as its own system user in a sandbox.
- The tray and `check` get their readings from the service, and say when they can't reach it.

### Changed

- The udev rule now gives receivers and cables to the service instead of to your account.

### Fixed

- A fresh install no longer reports a failure from the service.
- The tray keeps UPower's reading when UPower goes away.

### Security

- Programs you run can no longer open your receiver or cable, which also closes the chance of reaching a keyboard paired to the same receiver.
- Installing removes the access that earlier versions gave to receivers that are already plugged in.

## 1.0.1, 2026-10-01

### Added

- More receivers are covered, including the LIGHTSPEED `046d:c547` and the Logi Bolt `046d:c548`.
- Release files include a `SHA256SUMS` file.

### Changed

- The README says plainly what the udev rule allows, including that a program you run could send HID++ commands to a keyboard paired to the same receiver.

### Fixed

- Low battery warnings arm again after a charge, come again after a login or an update while the mouse is still low, and are tried again when they couldn't be delivered.
- Mice read through UPower no longer vanish from the menu when UPower is slow.
- Open at Login follows GNOME's own autostart setting.
- The first notification works on Qt 6.4.
- Mouse lines name a charging error, and a reading from an earlier day shows its date.
- The About box opens once, and Ctrl+C ends the app.
- Reading a mouse is more reliable.

### Security

- The packages install the app files with safer permissions.
- Device names have control characters removed before they're shown.

## 1.0.0, 2026-09-28

### Added

- A tray icon shows each connected Logitech mouse's battery, green while charging, red at 20% or below, and gray while asleep.
- The menu lists every mouse, and the app follows the one you're using.
- A warning arrives at 10% and again at 5% on each charge.
- Mice the kernel drives are read through UPower, and mice it leaves alone, such as the PRO X3 SUPERSTRIKE, are read over Logitech's HID++ protocol.
- A udev rule gives access only to HID++ interfaces that pass the app's own check.
- The app opens at login, and each account can turn that off from the menu.
- On GNOME, a notification offers to turn on the AppIndicator extension when there's no tray.
- `logitech-mouse-battery check` prints each mouse's battery in a terminal.
- Packages are available for Debian and Ubuntu (`.deb`), Fedora (`.rpm`), and Arch (`.pkg.tar.zst`).
