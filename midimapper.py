#!/home/krid/bin/pyvirtenv/bin/python3
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

import functools
import sys
import argparse
import time
import logging
from collections import namedtuple
from subprocess import run, CalledProcessError

from alsa_midi import SequencerClient, NoteOnEvent, NoteOffEvent, \
    ControlChangeEvent, PortUnsubscribedEvent

import Xlib.X
import Xlib.XK
import Xlib.display
# This isn't used, but it needs to be loaded to prime the extension.
import Xlib.ext.xtest
import Xlib.keysymdef.latin1
import Xlib.keysymdef.miscellany

Action = namedtuple("Action", ('keyspec', 'desc'))
Button = namedtuple("Button", ('control', 'action'))
Spinner = namedtuple("Spinner", ('control', 'action_up', 'action_down'))
Slider = namedtuple("Slider",
    ('control', 'delta', 'action_up', 'action_down', 'action_zero'))
# A one-tuple is still a tuple...
Cmd = namedtuple("Cmd", ('arg_list',))

# Special easter egg action
BOING = Action('Boing', 'Boing')

# <ControlChangeEvent channel=10 param=2 value=2>
# <ControlChangeEvent channel=10 param=2 value=1>
# <NoteOnEvent channel=10 note=0 velocity=127>
# <NoteOffEvent channel=10 note=0 velocity=0>
# <NoteOnEvent channel=10 note=8 velocity=127>
#
# https://python-alsa-midi.readthedocs.io/en/latest/api_events.html
#
# MAPPING = {
#     (EventType, param, value): Input(
#         "controller-input-name",
#         <key spec>,
#         "key spec description"
#     )
# }
#
# Where <key spec> is one of:
#
# str => Emit this key (see /usr/include/X11/keysymdef.h)
# function => run the function (for strange / complex stuff)
# list => Emit multiple keys or chords
#
# Multi-key list elements:
#
# tuple => emit chord
# str => emit key
# int => wait X miliseconds
#
# See /usr/include/X11/keysymdef.h for keycodes
NOTE_MAPPING = {
    0: Button("push-controller-1", Action('Delete', 'Delete')),
    8: Button("button-1", Action([('Meta_L', '3'), 'Right'], 'Flag Green')),
    9: Button("button-2", Action([('Meta_L', '2'), 'Right'], 'Flag Yellow')),
    10: Button("button-3", Action([('Meta_L', '1'), 'Right'], 'Flag Red')),
    11: Button("button-4", Action([('Meta_L', '0')], 'Clear Flag')),
    12: Button("button-5", Action(Cmd(["toggle-grayscale.sh"]), 'Toggle Greyscale')),
    13: Button("button-6", Action('Delete', 'Delete')),
    #13: Button("button-6", Action([('Control_L', 'Alt_L', '1')], 'Red')),
    14: Button("button-7", Action('Escape', 'Escape')),
    15: Button("button-8", Action('F3', 'Preview')),
    16: Button("button-9", Action([('Control_L', '1')], 'One Star')),
    17: Button("button-10", Action([('Control_L', '2')], 'Two Stars')),
    18: Button("button-11", Action([('Control_L', '3')], 'Three Stars')),
    19: Button("button-12", Action([('Control_L', '0')], 'Zero Stars')),
    20: Button("button-13", BOING),
    21: Button("button-22", Action([('Control_L', 'slash')], 'Flip Vertical')),
    22: Button("button-23", Action([('Control_L', 'Shift_L', 'asterisk')], 'Flip Horizontal')),
    23: Button("button-24", Action([('Control_L', 'period')], 'Toggle Zoom')),
    #: Button("button-", Action()),
}
CONTROL_MAPPING = {
    1: Spinner('spinner-1',
                Action('Right', 'Move Right'),
                Action('Left', 'Move Left')),
    2: Spinner('spinner-2',
                Action('Up', 'Move Up'),
                Action('Down', 'Move Down')),
    6: Spinner('spinner-6',
                Action([('Control_L', 'Shift_R', 'Right')], 'Rotate Right'),
                Action([('Control_L', 'Shift_R', 'Left')], 'Rotate Left'),
                ),
    7: Spinner('spinner-7',
                Action([('Shift_R', 'Left')], 'Pan Zoom Left'),
                Action([('Shift_R', 'Right')], 'Pan Zoom Right')),
    8: Spinner('spinner-8',
                Action([('Shift_R', 'Up')], 'Pan Zoom Up'),
                Action([('Shift_R', 'Down')], 'Pan Zoom')),
    # We want ~6 levels of zoom; 127 / 6 = 21
    9: Slider('slider', 5,
                Action([('Control_L', 'equal')], 'Zoom In'),
                Action([('Control_L', 'minus')], 'Zoom Out'),
                Action([('Control_L', 'Alt_L', 'E')], 'Fit to Window')),
}

# What device are we looking at?
CONTROLLER_DEVICE = "X-TOUCH MINI"


class Program:

    control_default = 64

    def __init__(self, dry_run) -> None:
        self.dry_run = dry_run
        self.state = {}
    def init_knobs(self):
        """Set all the knob controllers to their middle value, the fun way.
        """
        def do_strobe(val):
            for param in range(1, 9):
                nevt = ControlChangeEvent(channel=10, param=param, value=val)
                self.client.event_output(nevt, port=self.port)
            if val % 16 == 0:
                note1 = (val // 16) + 8
                note2 = 23 - (val // 16)
                nevt = NoteOnEvent(channel=10, note=note1)
                self.client.event_output(nevt, port=self.port)
                nevt = NoteOffEvent(channel=10, note=note1 - 1)
                self.client.event_output(nevt, port=self.port)
                nevt = NoteOnEvent(channel=10, note=note2)
                self.client.event_output(nevt, port=self.port)
                nevt = NoteOffEvent(channel=10, note=note2 + 1)
                self.client.event_output(nevt, port=self.port)
            self.client.drain_output()
            time.sleep(0.007)
        for val in range(0, 127, 4):
            do_strobe(val)
        for val in range(128, 0, -4):
            do_strobe(val)
        for param in range(1, 9):
            nevt = ControlChangeEvent(channel=10, param=param,
                value=self.control_default)
            self.client.event_output(nevt, port=self.port)
            nevt = NoteOffEvent(channel=10, note=param + 7)
            self.client.event_output(nevt, port=self.port)
            nevt = NoteOffEvent(channel=10, note=param + 15)
            self.client.event_output(nevt, port=self.port)
        self.client.drain_output()

    def set_control(self, param, new_value):
        nevt = ControlChangeEvent(channel=10, param=param, value=new_value)
        self.client.event_output(nevt, port=self.port)
        self.client.drain_output()

    @functools.lru_cache()
    def keysym2code(self, key):
        rv = self.display.keysym_to_keycode(Xlib.XK.string_to_keysym(key))
        if not rv:              # I think it returns 0, but maybe None
            raise Exception("No keycode for keysym '{}'".format(key))
        return rv

    def send_chord(self, keys):
        logging.debug("chord=%s", keys)
        keycodes = list(map(self.keysym2code, keys))
        if self.dry_run:
            return
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
        if self.dry_run:
            return
        self.display.xtest_fake_input(Xlib.X.KeyPress, keycode)
        self.display.xtest_fake_input(Xlib.X.KeyRelease, keycode)
        self.display.flush()

    def handle_slider(self, slider, event):
        # control, delta, value, action_up, action_down, action_zero
        prev_value = self.state.get(slider.control)
        if event.value == 0 and slider.action_zero is not None:
            self.state[slider.control] = 0
            return slider.action_zero
        if prev_value is None:
            # We have no way of knowing where the slider was before...
            self.state[slider.control] = event.value
            return None
        if event.value < prev_value - slider.delta:
            self.state[slider.control] = event.value
            return slider.action_down
        if event.value == 127 or event.value > prev_value + slider.delta:
            self.state[slider.control] = event.value
            return slider.action_up
        return None

    def handle_spinner(self, spinner, event):
        # control, value, action_up, action_down
        prev_value = self.state.get(spinner.control, self.control_default)
        new_value = event.value
        retval = None
        if new_value in (0, 127):
            # Spinner has hit the end of its range, wrap it around.
            # Outputting a new event will:
            # - Change the current value of the knob
            # - Change the LED lights correspondingly
            if new_value == 0:
                new_value = 127
                retval = spinner.action_down
            else:
                new_value = 0
                retval = spinner.action_up
            self.set_control(event.param, new_value)
        elif new_value < prev_value:
            retval = spinner.action_down
        elif new_value > prev_value:
            retval = spinner.action_up
        self.state[spinner.control] = new_value
        return retval

    def run_command(self, cmd_spec):
        try:
            run(cmd_spec.arg_list)
        except CalledProcessError:
            logging.info("Command '%s' failed.", cmd_spec.arg_list[0])

    def do_action(self, action):
        if action == BOING:
            self.init_knobs()
            return
        logging.info("Action %s => %s", action.desc, action.keyspec)
        if not action.keyspec:
            pass
        if isinstance(action.keyspec, Cmd):
            self.run_command(action.keyspec)
        elif isinstance(action.keyspec, list):
            for elem in action.keyspec:
                if isinstance(elem, str):
                    self.send_key(elem)
                elif isinstance(elem, int):
                    time.sleep(elem)
                elif isinstance(elem, tuple):
                    self.send_chord(elem)
                else:
                    raise Exception(
                        "Unsupported keyspec of type {} in Action {}".
                        format(type(elem), action))
        elif isinstance(action.keyspec, str):
            self.send_key(action.keyspec)

    def run(self):
        self.display = Xlib.display.Display()
        ext = self.display.query_extension('XTEST')
        if ext is None:
            raise Exception("Cannot get XTEST extension")

        # Connect to the MIDI device.
        # FIXME need to understand this stuff better.
        self.client = SequencerClient(CONTROLLER_DEVICE)
        self.port = self.client.create_port("inout")
        self.port.connect_from(self.client.list_ports()[0])
        self.port.connect_to(self.client.list_ports()[0])
        logging.info("Mapping inputs from %s", CONTROLLER_DEVICE)

        self.init_knobs()

        while True:
            event = self.client.event_input()
            logging.debug(event)
            action = None
            if event.type == PortUnsubscribedEvent.type:
                # Controller disconnected.
                # For now we gracefully exit.
                # TODO Consider waiting for a reconnection?
                logging.info("Controller '%s' disconnected or unavailable.",
                            CONTROLLER_DEVICE)
                sys.exit(0)
            elif event.type == NoteOnEvent.type:
                # We trigger on button release, and ignore the press
                continue
            elif event.type == NoteOffEvent.type:
                # A button release, which maps directly to an Action
                handler = NOTE_MAPPING.get(event.note)
                if handler:
                    action = handler.action
            elif event.type == ControlChangeEvent.type:
                # Spinner or slider event, which requires some interpretation
                # to determine the correct action.
                handler = CONTROL_MAPPING.get(event.param)
                if isinstance(handler, Spinner):
                    action = self.handle_spinner(handler, event)
                elif isinstance(handler, Slider):
                    action = self.handle_slider(handler, event)
                elif handler is not None:
                    logging.error("Unsupported handler type '%s'.", handler)
            else:
                logging.debug("Unsupported event type '%s'.", event.type)
            if action:
                self.do_action(action)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", "-d", action="store_true",
        help="Show debug info and unused controller inputs")
    parser.add_argument("--dry-run", "-n", action="store_true",
        help="Don't actually emit X keystrokes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s")

    Program(args.dry_run).run()
