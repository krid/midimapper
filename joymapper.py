#!/usr/bin/env python3
#
# Copyright (C) 2022-2024 Dirk Bergstrom <dirk@otisbean.com>. All Rights Reserved.
#
# Xlib code for keystroke generation originally by Joel Holveck <joelh@piquan.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import errno
import functools
import select
import struct
import sys
import argparse
import time
import logging
from collections import namedtuple

import Xlib.X
import Xlib.XK
import Xlib.display
# This isn't used, but it needs to be loaded to prime the extension.
import Xlib.ext.xtest
import Xlib.keysymdef.latin1
import Xlib.keysymdef.miscellany

Input = namedtuple("Input", ('control', 'keyspec', 'desc'))

# MAPPING = {
#     (typ, number, value): Input(
#         "controller-input-name",
#         <key spec>,
#         "key spec description"
#     )
# }
#
# Where:
#   <typ> is 1 (on/off button) or 2 (stick / trigger / throttle / etc)
#   <number> varies between manufacturers; use "jstest /dev/input/js0"
#     The numbers shown below work for most, but not all, gamepads.
#   <value> is the value that triggers the behavior.
#
# Where <key spec> is one of:
#
# str => Emit this key (see keysymdef.h)
# function => run the function (for strange / complex stuff)
# list => Emit multiple keys or chords
#
# Multi-key list elements:
#
# tuple => emit chord
# str => emit key
# int => wait X miliseconds
#
# I don't need the sticks/throttles to be graduated, so I'm treating them as
# switches.  If it turns out that having to jam them to the stops is annoying
# I can put in some slop & filtering.
#
# This doesn't allow for auto-repeat.  According to Joel there's a few ways to
# make that happen:
#
# * Send the key-down events when the button is pushed (1), and the key-up events
#   when it's released (0).  X itself will handle auto-repeat.
#
# * Do auto-repeat in my own code.
#
# * The joystick drivers supposedly support some level of auto-repeat.  You
#   need to set an ioctl to turn it on.  Joel knows how...
#
# See /usr/include/X11/keysymdef.h (from x11proto-core-dev) for keycodes.
# There's also some multimedia keys in XF86keysym.h.
#
# FIXME: Read from a config file
MAPPING = {
    (2, 8, 32767): Input('dpad-right', 'Right', 'Right'),
    (2, 8, -32767): Input('dpad-left', 'Left', 'Left'),
    (2, 9, -32767): Input('dpad-up', 'Up', 'Up'),
    (2, 9, 32767): Input('dpad-down', 'Down', 'Down'),
    (1, 0, 1): Input("a", [('Meta_L', '3')], 'Flag Green'),
    (1, 1, 1): Input("b", [('Meta_L', '1')], 'Flag Red'),
    (1, 2, 1): Input("x", [('Meta_L', '0')], 'Clear Flag'),
    (1, 3, 1): Input("y", [('Meta_L', '2')], 'Flag Yellow'),
    (1, 4, 1): Input("l-trigger", 'Escape', 'Escape'),
    (1, 5, 1): Input("r-trigger", 'F3', 'Preview'),
    (1, 7, 1): Input("l-hat", None, None),
    (1, 8, 1): Input("r-hat", None, None),
    (1, 6, 1): Input("play", 'Delete', 'Delete'),
    (2, 7, 32767): Input('l-throttle', None, None),  # Was [(Shift_R, Right), (Control_L, L)], 'Add to light table'
    (2, 6, 32767): Input('r-throttle', [('Control_L', 'period')], 'Toggle Zoom'),
    (2, 5, -32767): Input('r-joystick-up', [('Control_L', '3')], 'Three Stars'),
    (2, 5, 32767): Input('r-joystick-down', [('Control_L', '1')], 'One Star'),
    (2, 2, -32767): Input('r-joystick-left', [('Control_L', '0')], 'Zero Stars'),
    (2, 2, 32767): Input('r-joystick-right', [('Control_L', '2')], 'Two Stars'),
    (2, 1, -32767): Input('l-joystick-up', [('Shift_R', 'Up')], 'Pan Zoom'),
    (2, 1, 32767): Input('l-joystick-down', [('Shift_R', 'Down')], 'Pan Zoom'),
    (2, 0, -32767): Input('l-joystick-left', [('Shift_R', 'Left')], 'Pan Zoom'),
    (2, 0, 32767): Input('l-joystick-right', [('Shift_R', 'Right')], 'Pan Zoom'),
}

# What device are we looking at?
DEFAULT_CONTROLLER_DEVICE = '/dev/input/js0'

# Format of the packets we get from /dev/input/js0
# See below where we call .unpack
INPUT_STRUCT = struct.Struct('IhBB')


class Program:

    def __init__(self, controller):
        self.controller = controller

    @functools.cache()
    def keysym2code(self, key):
        rv = self.display.keysym_to_keycode(Xlib.XK.string_to_keysym(key))
        if not rv:              # I think it returns 0, but maybe None
            raise Exception("No keycode for keysym '{}'".format(key))
        return rv

    def send_chord(self, keys):
        logging.debug("chord=%s", keys)
        keycodes = list(map(self.keysym2code, keys))
        # We set the times with 10ms between the modifiers, the final
        # key, and the release.  (The default for the last argument is
        # X.CurrentTime.)
        for k in keycodes[:-1]:
            self.display.xtest_fake_input(Xlib.X.KeyPress, k, 0)
        self.display.xtest_fake_input(Xlib.X.KeyPress, keycodes[-1], 10)
        for k in reversed(keycodes):
            self.display.xtest_fake_input(Xlib.X.KeyRelease, k, 20)
        # Send the event stream.
        self.display.flush()

    def send_key(self, key):
        logging.debug("key=%s", key)
        keycode = self.keysym2code(key)
        self.display.xtest_fake_input(Xlib.X.KeyPress, keycode)
        self.display.xtest_fake_input(Xlib.X.KeyRelease, keycode)
        self.display.flush()

    def handle_x(self):
        # We don't need to process any X events, but we do need to
        # read the X responses to prevent them from piling up.
        for _ in range(self.display.pending_events()):
            self.display.next_event()

    def handle_js(self):
        evbuf = None
        try:
            evbuf = self.jsdev.read(INPUT_STRUCT.size)
        except OSError as ose:
            if ose.errno == errno.ENODEV:
                # No such device == disconnected
                pass
            else:
                raise
        if not evbuf:
            # In practice we only get here in the ENODEV case
            logging.info("Controller disconnected or unavailable at '%s'.",
                         self.controller)
            sys.exit(0)

        # *** THIS IS WHERE WE DISPATCH JOYSTICK STUFF TO X STUFF ***
        # To read the joystick, see:
        #     https://www.kernel.org/doc/Documentation/input/joystick-api.txt
        #     linux/joystick.h
        #     linux/input.h
        #     https://gist.github.com/rdb/8864666.
        # To see the names of X keysyms, see:
        #     X11/keysymdef.h
        # The Python keysymdef modules are based on the preceding #ifdef.
        _time, value, typ, number = INPUT_STRUCT.unpack(evbuf)
        inp = MAPPING.get((typ, number, value))
        if inp:
            logging.info("Controller event %s => %s", inp.control, inp.desc)
            if not inp.keyspec:
                pass
            if isinstance(inp.keyspec, list):
                for elem in inp.keyspec:
                    if isinstance(elem, str):
                        self.send_key(elem)
                    elif isinstance(elem, int):
                        time.sleep(elem)
                    elif isinstance(elem, tuple):
                        self.send_chord(elem)
                    else:
                        raise Exception(
                            "Unsupported keyspec of type {} in Input {}".
                            format(type(elem), inp))
            elif isinstance(inp.keyspec, str):
                self.send_key(inp.keyspec)
        elif abs(value) in (1, 32767):
            logging.debug("Controller event: type %d number %d value %d",
                          typ, number, value)

    def run(self):
        self.display = Xlib.display.Display()
        ext = self.display.query_extension('XTEST')
        if ext is None:
            raise Exception("Cannot get XTEST extension")

        self.jsdev = open(self.controller, 'rb')
        logging.info("Mapping inputs from %s", self.controller)

        while True:
            (ready_to_read, _, _) = select.select(
                [self.display, self.jsdev], [], [])

            if self.display in ready_to_read:
                self.handle_x()

            if self.jsdev in ready_to_read:
                self.handle_js()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", action="store",
        default=DEFAULT_CONTROLLER_DEVICE,
        help="Listen to this input")
    parser.add_argument("--debug", "-d", action="store_true",
        help="Show debug info and unused controller inputs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s")

    Program(controller=args.controller).run()
