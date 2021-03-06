pulseaudio-mixer-cli
--------------------

Interactive ncurses UI to control volume of pulse streams.

Kinda like alsamixer, but focused not on sink volume levels (which can actually
be controlled via alsamixer, with alsa-pulse plugin), but rather on volume of
individual streams, so you can turn down the music to hear the stuff from game,
mumble, skype or flash.

Control over individual process streams seem to be almost unique to pulseaudio,
pity there aren't much tools built to harness it (at least weren't,
initially). This one tries to fill the gap a bit.

Thanks to the most awesome contributors, the tool is now useable with
system-wide pulseaudio instance, can mute streams/sinks, works with vi-style
keys as well as cursor and has many other fixes and features.


Installation
--------------------

Just copy the script to wherever is convenient (~/bin or /usr/local/bin comes to
mind), do a "chmod +x" on it, run.

Make sure you have `load-module module-dbus-protocol` line in
/etc/pulse/default.pa (or /etc/pulse/system.pa, if system-wide daemon is used),
especially on Ubuntu, where it seem to be disabled by default
(see [#1](https://github.com/mk-fg/pulseaudio-mixer-cli/issues/1)).

There is now also "pa-mixer-mk2.py" script in the repo, which is a rewrite of
the original version, and might have some extra features and/or bugs.

### Requirements

* Python 2.7
* dbus-python (standard python dbus bindings)
* PyGObject (aka PyGI, likely installed if dbus-python is)
* PulseAudio 1.0+


Usage
--------------------

Run the script (either "pulseaudio-mixer-cli.py" or "pa-mixer-mk2.py") with "-h"
or "--help" option to see various other options, but there aren't that many -
most stuff is configurable via config file (described below).

That's basically how it looks... in an overly narrow terminal (to fit on a github
page), and without "inverted row" selection visible:

	HDMI 0 (hdmi-stereo@snd_hda_intel)                - [ ############################### ]
	ID 440 Analog (analog-stereo@snd_hda_intel)       - [ ##########################----- ]
	mpv - Bax - Perceptions 206 on ETN.fm Jan-22-2015 - [ #########---------------------- ]
	VLC media player (fraggod@malediction:24321)      - [ ##############----------------- ]
	Skype (fraggod@malediction:24202)                 - [ #################-------------- ]
	ALSA Playback                                     M [ #######------------------------ ]

Sink levels always displayed on top, "M" or "-" to the left of the bar is a mute
indicator.

Controls are arrow keys (incl. numpad) or their vi/emacs-style counterparts to
pick row and adjust bars left and right, "m" or "space" to toggle mute, "q" to
quit and "1"-"0" number row keys to set specific level (1=0%, 0=100%, only in
pa-mixer-mk2).

Script can read simple ini-like config from "~/.pulseauido-mixer-cli.cfg".
See [RawConfigParser docs](http://docs.python.org/2/library/configparser.html)
for more details on format of that file.

For example:

	[default]
	adjust-step: 2
	max-level: 131072
	use-media-name: true
	focus-default: last
	focus-new-items: false

Such config is totally optional, and might be useful in case default options
aren't suitable for a specific setup.
See "pa-mixer-mk2.example.cfg" for the full list of these.
Commandline values (where available) override the ones defined in the config file.

There is a shiny rewritten "pa-mixer-mk2.py" script version, which is probably
way less tested, but have some extra features, which I can't be bothered to
add/test for an old one, so maybe take a look at that one as well.


Internals
--------------------

Since I wasn't able to easily couple ncurses eventloop with glib/dbus one (which
should poll for async signals), I settled on splitting glib loop into it's own
process.
Both loops communicate via pipes, opened before fork(), waking each other up
from the respective loop (to process data being sent via pipes) when necessary
with POSIX signals.

Pulseaudio dbus interface was introduced in 1.0-dev branch (which is actually
fairly old), but was merged mid-2011 into mainline versions.
More documentation on it can be found via introspection or on [PA
wiki](http://pulseaudio.org/wiki/DBusInterface).

Since interface processes signals about new/removed streams and sinks, and not
just polls the data on some intervals, it should be fairly responsive to these
changes.
There are signals for volume updates, but they aren't processed just for the
sake of simplicity. Volume levels are polled on occasional changes anyway, so
they should be updated on the ui update events.

DBus reconnection (sometimes via re-exec, because python-dbus seem to cache more
stuff than it probably should) is built-in, so there should be no problem with
transient pulseaudio processes, although the fact that the client is connected
via dbus interface might keep them alive indefinitely.

Starting the mixer should also trigger pulseaudio start, if proper dbus
autolaunch service descriptions are installed in the system.

Script should also work with system-wide pulseaudio daemon (usage of which is
[highly discouraged by developers](http://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/WhatIsWrongWithSystemWide),
btw) - in that case neither dbus system nor session bus is accessed, since
ServerLookup interface doesn't seem to be available on either one (at least in
2.1), and pa-private bus is accessed via well-known socket location at
/run/pulse/dbus-socket (see also [#4](https://github.com/mk-fg/pulseaudio-mixer-cli/issues/4)).
