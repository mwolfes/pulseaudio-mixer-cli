#!/usr/bin/python
from __future__ import unicode_literals, print_function

import os, sys

defaults = {'adjust-step': 5, 'max-level': 2 ** 16, 'encoding': 'utf-8',
            'use-media-name': False, 'verbose': False, 'debug': False}

# Read configuration file, if any
try:
    # Python 3
    import configparser
except ImportError:
    # Python 2
    import ConfigParser as configparser

try:
    with open(os.path.expanduser('~/.pulseaudio-mixer-cli.cfg')) as src:
        config = configparser.SafeConfigParser(allow_no_value=True)
        config.readfp(src)
except (OSError, IOError): pass
else:
    for k, v in defaults.items():
        get_val = config.getint if not isinstance(v, bool) else config.getboolean
        try: defaults[k] = get_val('default', k)
        except configparser.Error: pass

import argparse
parser = argparse.ArgumentParser(description='Pulseaudio sound level control tool.')
parser.add_argument('-a', '--adjust-step',
                    action='store', type=int, metavar='step', default=defaults['adjust-step'],
                    help='Adjustment for a single keypress in interactive mode (0-100%%, default: %(default)s%%).')
parser.add_argument('-l', '--max-level',
                    action='store', type=int, metavar='level', default=defaults['max-level'],
                    help='Value to treat as max (default: %(default)s).')
parser.add_argument('-n', '--use-media-name',
                    action='store_true', default=defaults['use-media-name'],
                    help='Display streams by "media.name" property, if possible.'
                    ' Default is to prefer application name and process properties.')
parser.add_argument('-e', '--encoding',
                    metavar='enc', default=defaults['encoding'],
                    help='Encoding to enforce for the output. Any non-decodeable bytes will be stripped.'
                    ' Mostly useful with --use-media-name. Default: %(default)s.')
parser.add_argument('-v', '--verbose',
                    action='store_true', default=defaults['verbose'],
                    help='Dont close stderr to see any sort of errors (which'
                    ' mess up curses interface, thus silenced that way by default).')
parser.add_argument('--debug',
                    action='store_true', default=defaults['debug'],
                    help='Verbose operation mode.')
optz = parser.parse_args()


import itertools as it, operator as op, functools as ft
from subprocess import Popen, PIPE, STDOUT
import dbus

if sys.version_info.major == 3:
    it.imap, unicode = map, str

import logging
logging.basicConfig(level=logging.DEBUG if optz.debug else logging.INFO)
log = logging.getLogger()

if not optz.verbose and not optz.debug:
    sys.stderr.close()  # so that output won't break the interface


def get_bus_address():
    srv_addr = os.environ.get('PULSE_DBUS_SERVER')
    if not srv_addr\
            and os.access('/run/pulse/dbus-socket', os.R_OK | os.W_OK):
        # Well-known system-wide daemon socket
        srv_addr = 'unix:path=/run/pulse/dbus-socket'
    if not srv_addr:
        srv_addr = dbus.SessionBus().get_object(
            'org.PulseAudio1', '/org/pulseaudio/server_lookup1')\
            .Get('org.PulseAudio.ServerLookup1',
                 'Address', dbus_interface='org.freedesktop.DBus.Properties')
    return srv_addr


def get_bus(srv_addr=None, dont_start=False):
    while not srv_addr:
        try:
            srv_addr = get_bus_address()
            log.debug('Got pa-server bus from dbus: %s', srv_addr)
        except dbus.exceptions.DBusException as err:
            if dont_start or srv_addr is False or\
                    err.get_dbus_name() != 'org.freedesktop.DBus.Error.ServiceUnknown':
                raise
            Popen(['pulseaudio', '--start', '--log-target=syslog'],
                  stdout=open('/dev/null', 'w'), stderr=STDOUT).wait()
            log.debug('Started new pa-server instance')
            ## Contrary to docs, "pulseaudio --start" does not mean shit ;(
            from time import sleep
            sleep(1)
            srv_addr = False  # to avoid endless loop
    # print(dbus.connection.Connection(srv_addr)\
    #   .get_object(object_path='/org/pulseaudio/core1')\
    #   .Introspect(dbus_interface='org.freedesktop.DBus.Introspectable'))
    return dbus.connection.Connection(srv_addr)


from io import open
import signal

signal.signal(signal.SIGUSR1, signal.SIG_IGN)
fd_out, fd_in = os.pipe()
core_pid = os.getpid()
child_pid = os.fork()

if not child_pid:
    ### Stream changes monitoring thread
    # ...implemented as a subprocess
    # glib loop doesn't seem to release GIL and I don't know
    #  how to combine curses-input and glib loops otherwise

    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    os.close(fd_out)
    pipe = open(fd_in, 'wb', buffering=0)
    pipe.write(b'\n')  # wait for main process to get ready
    log.debug('DBus signal handler thread started')

    DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    signal.signal(signal.SIGUSR1, lambda sig, frm: loop.quit())

    def notify(path, op):
        try:
            os.kill(core_pid, signal.SIGUSR1)
            pipe.write('{} {}\n'.format(op, path).encode('utf-8'))
        except:
            loop.quit()

    while True:
        bus = get_bus()
        core = bus.get_object(object_path='/org/pulseaudio/core1')
        for sig_name, sig_handler in (
                ('NewSink', ft.partial(notify, op='^')),
                ('SinkRemoved', ft.partial(notify, op='v')),
                ('NewPlaybackStream', ft.partial(notify, op='+')),
                ('PlaybackStreamRemoved', ft.partial(notify, op='-'))):
            bus.add_signal_receiver(sig_handler, sig_name)
            core.ListenForSignal('org.PulseAudio.Core1.{}'
                .format(sig_name), dbus.Array(signature='o'))
        loop.run()

    # This should never be executed
    raise RuntimeError('Child code broke out of the loop')

else:
    os.close(fd_in)
    pipe = open(fd_out, 'rb', buffering=0)


### UI backend

from collections import deque
from time import time
import re  # for some templating


class PAUpdate(Exception):
    pass


class PAMenu(dict):
    # OrderedDict doesn't seem to handle clear+update correctly in py2.7
    updates = deque()
    _volume_val_cache = dict()
    _mute_val_cache = dict()

    def __init__(self, cache_time=2, fail_hook=None):
        self.fail_hook, self._cache_time = fail_hook, cache_time
        super(PAMenu, self).__init__()
        self.refresh(soft=False)
        signal.signal(signal.SIGUSR1, self.update_handler)
        pipe.readline()  # unblock child

    def _dbus_failsafe(method):
        def dbus_failsafe_method(self, *argz, **kwz):
            # It doesn't seem to matter how large pa-restart and
            #  dbus-reconnection delay is - something like UnknownMethod
            #  will be raised in this pid anyway - only restart works
            try:
                return method(self, *argz, **kwz)
            except dbus.exceptions.DBusException:
                try:
                    self.refresh()
                    return method(self, *argz, **kwz)
                except dbus.exceptions.DBusException as err:
                    log.debug('Fatal error accessing dbus: %s', err)
                    if self.fail_hook:
                        self.fail_hook()
        return dbus_failsafe_method

    def _dbus_dec(self, prop):
        return unicode(bytes(bytearray((_ for _ in prop if _))), optz.encoding, 'ignore')

    _unique_idx=it.cycle(it.takewhile(lambda _:_<2**30, it.count(0)))
    _placeholder_names = ['audio stream', 'AudioStream']

    def _get_name_unique(self, name):
        tpl = '{} #{}'
        if isinstance(name, bytes): tpl = tpl.encode(optz.encoding)
        return tpl.format(name, next(self._unique_idx))

    def _get_name(self, iface, props):
        # log.debug('\n'.join('{}: {}'.format(bytes(k), self._dbus_dec(v)) for k,v in props.items()))
        if iface == 'Stream':
            if optz.use_media_name:
                try:
                    name = self._dbus_dec(props['media.name'])
                    if name in self._placeholder_names: raise KeyError
                    return name
                except KeyError:
                    pass
            try:
                name = self._dbus_dec(props['application.name'])
            except KeyError:
                # Assuming some synthetic stream with non-descriptive name
                name = self._get_name_unique(self._dbus_dec(props['media.name']))
            ext = '({application.process.user}@'\
                '{application.process.host}:{application.process.id})'
        elif iface == 'Device':
            try:
                name = self._dbus_dec(props['alsa.id'])
            except KeyError:
                try:
                    name = '{}.{}'.format(*it.imap(self._dbus_dec,
                                                   [props['device.api'], props['device.string']]))
                except KeyError:
                    name = '{} #{}'.format(
                        self._dbus_dec(props['device.description']), next(self._unique_idx))
            ext = '({device.profile.name}@{alsa.driver_name})'
        else:
            raise KeyError('Unknown interface (for naming): {}'.format(iface))
        try:
            name = '{} {}'.format(name, re.sub(r'\{([^}]+)\}', r'{}', ext)
                                  .format(*it.imap(self._dbus_dec, op.itemgetter(*re.findall(r'\{([^}]+)\}', ext))(props))))
        except KeyError:
            pass
        return name

    @_dbus_failsafe
    def add(self, path, iface):
        stream = self.bus.get_object(object_path=path)
        stream_props = dict(stream.Get(
            'org.PulseAudio.Core1.{}'.format(iface), 'PropertyList'))
        name = self._get_name(iface, stream_props)
        if optz.use_media_name and name in self:
            # Names can be duplicate here, as no client id get added - they are long enough as it is
            self[self._get_name_unique(name)] = self.pop(name)
            name = self._get_name_unique(name)
        self[name] = iface, stream
        if len(name) > self.max_key_len:
            self.max_key_len = len(name)
        return name

    @_dbus_failsafe
    def remove(self, path):
        for name, (iface, obj) in self.items():
            if obj.object_path == path:
                break
        else:
            return
        del self[name]
        if len(name) == self.max_key_len:
            self.max_key_len = max(it.imap(len, self)) if self else 0

    def refresh(self, soft=True):
        log.debug('PA-refresh initiated')
        if not soft:
            self.clear()
            self.bus = get_bus()
        self._volume_val_cache.clear()
        self._mute_val_cache.clear()
        self.max_key_len = 0  # should be recalculated from these entries only
        try:
            stream_names = set(
                self.add(path, 'Stream') for path in
                self.bus.get_object(object_path='/org/pulseaudio/core1')
                    .Get('org.PulseAudio.Core1', 'PlaybackStreams',
                         dbus_interface='org.freedesktop.DBus.Properties'))
            sink_names = set(
                self.add(path, 'Device') for path in
                self.bus.get_object(object_path='/org/pulseaudio/core1')
                    .Get('org.PulseAudio.Core1', 'Sinks',
                         dbus_interface='org.freedesktop.DBus.Properties'))
        except dbus.exceptions.DBusException:  # bus is probably abandoned
            if soft:
                self.refresh(soft=False)
            else:
                raise
        else:
            if not soft:
                os.kill(child_pid, signal.SIGUSR1)  # break glib loop to reacquire the bus
            else:
                # self.remove checks are not needed here
                for name in stream_names.difference(self):
                    del self[name]

    def update(self):
        while self.updates:
            action, path = self.updates.popleft()
            {'+': ft.partial(self.add, iface='Stream'), '-': self.remove,
             '^': ft.partial(self.add, iface='Sink'), 'v': self.remove}[action](path)

    def update_handler(self, sig, frm):
        try:
            self.updates.append(pipe.readline().decode('utf-8').strip().split(' ', 1))
        except IOError:
            reexec()  # child's dead

    @_dbus_failsafe
    def _get_volume(self, item):
        iface, obj = self[item]
        return obj.Get('org.PulseAudio.Core1.{}'.format(iface), 'Volume')

    def get_volume(self, item, raw=False):
        try:
            val, ts = self._volume_val_cache[item]
        except KeyError:
            val = None
        ts_chk = time()
        if val is None or ts < ts_chk - self._cache_time:
            try:
                val = self._get_volume(item)
            except KeyError:
                raise PAUpdate
            val = tuple(min(op.truediv(val, optz.max_level), 1.0) for val in val)
            self._volume_val_cache[item] = val, ts_chk
        return (sum(val) / len(val)) if not raw else val  # average of channels

    @_dbus_failsafe
    def _set_volume(self, item, val):
        iface, obj = self[item]
        return obj.Set('org.PulseAudio.Core1.{}'.format(iface),
                       'Volume', val, dbus_interface='org.freedesktop.DBus.Properties')

    def set_volume(self, item, val):
        # all channels to the same level
        val = [max(0, min(1, val))] * len(self.get_volume(item, raw=True))

        val_dbus = list(dbus.UInt32(round(val * optz.max_level)) for val in val)
        try:
            self._set_volume(item, val_dbus)
        except KeyError:
            raise PAUpdate
        self._volume_val_cache[item] = val, time()

    @_dbus_failsafe
    def _get_mute(self, item):
        iface, obj = self[item]
        return obj.Get('org.PulseAudio.Core1.{}'.format(iface), 'Mute')

    def get_mute(self, item):
        try:
            val, ts = self._mute_val_cache[item]
        except KeyError:
            val = None
        ts_chk = time()
        if val is None or ts < ts_chk - self._cache_time:
            try:
                val = self._get_mute(item)
            except KeyError:
                raise PAUpdate
            self._mute_val_cache[item] = val, ts_chk
        return val

    @_dbus_failsafe
    def _set_mute(self, item, val):
        iface, obj = self[item]
        return obj.Set('org.PulseAudio.Core1.{}'.format(iface),
                       'Mute', val, dbus_interface='org.freedesktop.DBus.Properties')

    def set_mute(self, item, val):
        val_dbus = dbus.Boolean(val)
        try:
            self._set_mute(item, val_dbus)
        except KeyError:
            raise PAUpdate
        self._mute_val_cache[item] = val, time()

    def next_key(self, item):
        try:
            return (list(it.dropwhile(lambda k: k != item, self)) + list(self) * 2)[1]
        except IndexError:
            return ''

    def prev_key(self, item):
        try:
            return (list(it.dropwhile(lambda k: k != item,
                                      reversed(self))) + list(reversed(self)) * 2)[1]
        except IndexError:
            return ''

    def _sort_key(self, item):
        name, (iface, obj) = item
        return iface, name

    def __iter__(self, reverse=False):
        return iter(it.imap(op.itemgetter(0),
                            sorted(self.items(), key=self._sort_key, reverse=reverse)))

    def __reversed__(self):
        return self.__iter__(reverse=True)

    def __del__(self):
        try:
            os.kill(child_pid, signal.SIGTERM)
        except OSError:
            pass


### UI rendering / input loop

from curses import wrapper
import curses


def interactive_cli(stdscr, items, border=0):
    curses.curs_set(0)
    curses.use_default_colors()

    def win_size():
        size = stdscr.getmaxyx()
        return max(1, size[0] - 2 * border), max(1, size[1] - 2 * border)

    def win_draw(win, items, hl=None,
                 item_len_min=10, bar_len_min=10,
                 bar_caps=lambda bar='': ' [ ' + bar + ' ]'):
        win_rows, win_len = win.getmaxyx()
        if win_len <= 1:
            return

        item_len_max = items.max_key_len
        mute_button_len = 2
        bar_len = win_len - item_len_max - mute_button_len - len(bar_caps())
        if bar_len < bar_len_min:
            item_len_max = max(item_len_min,
                               item_len_max + bar_len - bar_len_min)
            bar_len = win_len - item_len_max - mute_button_len - len(bar_caps())
            if bar_len <= 0:
                item_len_max = win_len  # just draw labels
            if item_len_max < item_len_min:
                item_len_max = min(items.max_key_len, win_len)

        win.erase()  # cleanup old entries
        for row, item in enumerate(items):
            if row >= win_rows - 1:
                # Not sure why bottom window row seem to be unuseable
                break

            attrs = curses.A_REVERSE if item == hl else curses.A_NORMAL

            win.addstr(row, 0, item[:item_len_max].encode(optz.encoding), attrs)
            if win_len > item_len_max + mute_button_len:
                if items.get_mute(item):
                    mute_button = " M"
                else:
                    mute_button = " -"
                win.addstr(row, item_len_max, mute_button)

                if bar_len > 0:
                    bar_fill = int(round(items.get_volume(item) * bar_len))
                    bar = bar_caps('#' * bar_fill + '-' * (bar_len - bar_fill))
                    win.addstr(row, item_len_max + mute_button_len, bar)

    win = curses.newwin(*(win_size() + (border, border)))
    win.keypad(True)

    hl = next(iter(items)) if items else ''
    optz.adjust_step /= 100.0

    while True:
        if os.waitpid(child_pid, os.WNOHANG)[0]:
            log.fatal('DBus signal monitor died unexpectedly')
            sys.exit(1)

        while items.updates:
            items.update()
        if not items:
            items.refresh()

        try:
            win_draw(win, items, hl=hl)
        except PAUpdate:
            continue

        if items.updates:
            continue

        try:
            key = win.getch()
        except curses.error:
            continue
        log.debug('Keypress event: %s', key)

        try:
            if key in (curses.KEY_DOWN, ord('j'), ord('n')):
                hl = items.next_key(hl)
            elif key in (curses.KEY_UP, ord('k'), ord('p')):
                hl = items.prev_key(hl)
            elif key in (curses.KEY_LEFT, ord('h'), ord('b')):
                items.set_volume(hl, items.get_volume(hl) - optz.adjust_step)
            elif key in (curses.KEY_RIGHT, ord('l'), ord('f')):
                items.set_volume(hl, items.get_volume(hl) + optz.adjust_step)
            elif key in (ord(' '), ord('m')):
                items.set_mute(hl, not items.get_mute(hl))
            elif key < 255 and key > 0 and chr(key) == 'q':
                exit()
            elif key in (curses.KEY_RESIZE, ord('\f')):
                curses.endwin()
                stdscr.refresh()
                win = curses.newwin(*(win_size() + (border, border)))
                win.keypad(True)
        except PAUpdate:
            continue


def reexec():
    log.debug('Restarting the app due to some critical failure')
    try:
        os.kill(child_pid, signal.SIGKILL)  # to prevent it sending USR1 to new process
    except OSError:
        pass
    try:
        os.execv(__file__, sys.argv)
    except OSError:
        os.execvp('python', ['python', __file__] + sys.argv[1:])

wrapper(interactive_cli, items=PAMenu(fail_hook=reexec), border=1)


log.debug('Finished')
