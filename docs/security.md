# Privacy and permissions

This page covers what Mouse Battery's udev rule and battery service allow, for anyone who wants the details behind the short summary in the README.

## What the udev rule allows

The rule hands the HID++ interface of any Logitech USB device that the kernel leaves to its generic HID driver, such as a receiver or a mouse on its cable, to the `logitech-mouse-battery` group, when that interface's report descriptor passes the app's own check. The check rejects any interface with a keyboard, keypad, or mouse collection and any receiver interface that carries a paired keyboard's keystrokes. Only Mouse Battery's service runs in that group, as its own system user in a systemd sandbox, and it offers nothing but battery readings and each mouse's saved history on the system bus. Nothing this package installs lets a program you run open the receiver or cable, or send it HID++ commands.

## What any local account can do

Any local user may read the battery readings and each mouse's saved history, which include each mouse's name, its unit id, and when it last woke and was read, and the history shows when each mouse was in use. Any local user may also ask for a new read.

The service reads the mice at most once a second, with either a full read or a silent read (a read of only the mice that don't report on their own). Each account gets five reads at once and then one every 30 seconds, after which a read is turned down with an error. Reading a mouse's saved history has its own budget of the same size, and reading the levels for the chart gets ten at once and then one every 5 seconds.

These budgets are kept per account, with no limit across accounts, so someone with many local accounts could keep the service busy. That slows it down but doesn't change the saved history.

## Other programs and older versions

Another package's udev rule can still give your account access of its own, as Solaar's does for every Logitech receiver. A current Solaar keeps that access when Mouse Battery is installed. However, the older Solaar that Debian 12 and Ubuntu 24.04 ship loses it until the receiver is plugged in again or the computer restarts.

One limit stays after an upgrade from 1.0.x. A program that already had a receiver or cable open when the upgrade was installed keeps that open handle until it closes it, since Linux checks access when a file is opened. Closing that program, replugging the receiver or cable, or restarting the computer closes the handle.

Mouse Battery shows and saves the battery level UPower reports, the same as the desktop's own battery applet does. On a computer shared by several people, another local account that can talk to BlueZ may be able to change what UPower reports for a paired Bluetooth mouse, and that value would show in the tray and be saved in the history.

## After uninstalling

The service's group keeps its access to a plugged in receiver or cable until you replug it or restart, and the `logitech-mouse-battery` system user stays, as package managers leave system users in place.
