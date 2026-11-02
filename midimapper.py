#!/home/krid/bin/pyvirtenv/bin/python3
#
# Copyright (C) 2022-2026 Dirk Bergstrom <dirk@otisbean.com>. All Rights Reserved.
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
from evdev import UInput, ecodes as ec

Action = namedtuple("Action", ('keyspec', 'desc'))
Button = namedtuple("Button", ('control', 'action'))
Spinner = namedtuple("Spinner", ('control', 'action_up', 'action_down'))
Slider = namedtuple("Slider",
    ('control', 'delta', 'action_up', 'action_down', 'action_zero'))
# A one-tuple is still a tuple...
Cmd = namedtuple("Cmd", ('arg_list',))

# Special easter egg action
BOING = Action('Boing', 'Boing')

# NOTE_MAPPING
# This defines a mapping between a MIDI "note" (basically a button press)
# and some action (e.g. a keyboard shortcut).  It's a dict keyed on note ID,
# where each value is a Button namedtuple containing:
#
# ("button-name", Action())
#
# The Action, in turn is:
#
# (<key spec>, "human-readable description")
#
# Where <key spec> is one of:
#
# tuple => emit chord
# str => Emit this key (see /usr/include/X11/keysymdef.h)
# int => wait X miliseconds
# Cmd() => Run the given command-and-arguments list
# list => Emit a sequence of key specs
#
# TODO function => run the function (for strange / complex stuff)
#
# So this entry:
# 
# 8: Button("button-1", Action([('LeftAlt', '3'), 'RIGHT'], 'Flag Green')),
# 
# Translates to:
# 
#    When button 8 is released, log an event for `button-1`, and then send the
#    key combo `Alt-3`, immediately followed by the `right-arrow` key, and print
#    "Flag Green" to the console.
# 
# See uapi/linux/input-event-codes.h for keycodes
# Note: We're not including the 'KEY_' prefix here
NOTE_MAPPING = {
    0: Button("push-controller-1", Action('Delete', 'Delete')),
    8: Button("button-1", Action([('LeftAlt', '3'), 'Right'], 'Flag Green')),
    9: Button("button-2", Action([('LeftAlt', '2'), 'Right'], 'Flag Yellow')),
    10: Button("button-3", Action([('LeftAlt', '1'), 'Right'], 'Flag Red')),
    11: Button("button-4", Action([
        'LEFT', ('LeftAlt', '2'), 'Right', ('LeftAlt', '3')],
        'Flag Prev Y, Flag This G')),
    #11: Button("button-4", Action(('LeftAlt', '0'), 'Clear Flag')),
    12: Button("button-5", Action(Cmd(["toggle-grayscale.sh"]), 'Toggle Greyscale')),
    13: Button("button-6", Action('Delete', 'Delete')),
    #13: Button("button-6", Action([('RightCtrl', 'LeftAlt', '1')], 'Red')),
    14: Button("button-7", Action('Esc', 'Escape')),
    15: Button("button-8", Action('F3', 'Preview')),
    16: Button("button-9", Action(('LeftCtrl', '1'), 'One Star')),
    17: Button("button-10", Action(('LeftCtrl', '2'), 'Two Stars')),
    18: Button("button-11", Action(('LeftCtrl', '3'), 'Three Stars')),
    19: Button("button-12", Action(('LeftCtrl', '0'), 'Zero Stars')),
    20: Button("button-13", BOING),
    21: Button("button-22", Action(('LeftCtrl', 'SLASH'), 'Flip Vertical')),
    22: Button("button-23", Action(('LeftCtrl', 'LeftShift', '8'), 'Flip Horizontal')),
    23: Button("button-24", Action(('LeftCtrl', 'DOT'), 'Toggle Zoom')),
    #: Button("button-", Action()),
}

# CONTROL_MAPPING
# As above for NOTE_MAPPING, but this maps continuous controls like spinners
# and sliders.
#
# The `name` field is used as the key in the dict for storing control state, so
# it must be unique and should be diagnostic.
#
# Spinners have two Actions: clockwise/up and counter-clockwise/down.
# Spinners are implemented as continuous controls that wrap around, as opposed
# to hitting an imaginary stop.
# 
# Sliders have a `delta` argument which specifies how much change will
# trigger the effect, and three Actions: up, down, and return-to-zero
# (useful for resetting the slider).
#
# An Action may be `None`, in which case nothing will happen.
CONTROL_MAPPING = {
    1: Spinner('spinner-1',
                Action('Right', 'Move Right'),
                Action('Left', 'Move Left')),
    2: Spinner('spinner-2',
                Action('Up', 'Move Up'),
                Action('Down', 'Move Down')),
    6: Spinner('spinner-6',
                Action([('LeftCtrl', 'RightShift', 'Right')], 'Rotate Right'),
                Action([('LeftCtrl', 'RightShift', 'Left')], 'Rotate Left'),
                ),
    7: Spinner('spinner-7',
                Action([('RightShift', 'Left')], 'Pan Zoom Left'),
                Action([('RightShift', 'Right')], 'Pan Zoom Right')),
    8: Spinner('spinner-8',
                Action([('RightShift', 'Up')], 'Pan Zoom Up'),
                Action([('RightShift', 'Down')], 'Pan Zoom')),
    # We want ~6 levels of zoom; 127 / 6 = 21
    9: Slider('slider', 5,
                Action([('LeftCtrl', 'Equal')], 'Zoom In'),
                Action([('LeftCtrl', 'Minus')], 'Zoom Out'),
                Action([('LeftAlt', 'RightCtrl', 'E')], 'Fit to Window')),
}

# What device are we looking at?
CONTROLLER_DEVICE = "X-TOUCH MINI"
CONTROLLER_PORT_NAME = 'Midimapper Port'


class Program:
    """The action all happens here"""

    control_default = 64

    def __init__(self, dry_run) -> None:
        self.dry_run = dry_run
        self.state = {}

    def init_knobs(self):
        """Set all the knob controllers to their middle value, the fun way.

        The X-Touch MINI has some state memory, so the values of the inputs
        and their associated lights need to be set to a known state.  I got
        just a bit carried away implementing this code...
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
        """Set a MIDI control to a specific value.
        
        Used when wrapping a spinner around from 127 <=> 0.
        """
        nevt = ControlChangeEvent(channel=10, param=param, value=new_value)
        self.client.event_output(nevt, port=self.port)
        self.client.drain_output()

    def sendkey(self, key, up_or_down):
        """Send the key event, up=0, down=1.
        """
        self.uinput.write(ec.EV_KEY, ec.ecodes[f"KEY_{key.upper()}"], up_or_down)

    def send_chord(self, keys):
        """Send uinput key-down events for each of the keys, then send key-up
        events in reverse order.

        Note: A chord using the ALT and CTRL keys on the same side of the keyboard
#       seems to ignore the CTRL, whereas using them from opposite sides works?

        The X Input version needed a delay between the down & up events, but
        Uinput doesn't appear to care.
        """
        logging.debug("chord=%s", keys)
        if self.dry_run:
            return
        for k in keys:
            self.sendkey(k, 1)
        for k in reversed(keys):
            self.sendkey(k, 0)
        self.uinput.syn()

    def send_key(self, key):
        """Send a single keystroke via "press" and "release"."""
        logging.debug("key=%s", key)
        if self.dry_run:
            return
        self.sendkey(key, 1)
        self.sendkey(key, 0)
        self.uinput.syn()

    def handle_slider(self, slider_spec, event):
        """Handle a slider change event.
        
        If the difference between current and previous value exceeds
        slider_spec.delta, or the slider is changed to zero, the relevant
        Action is returned.  Otherwise we return None.
        """
        # control, delta, value, action_up, action_down, action_zero
        prev_value = self.state.get(slider_spec.control)
        if event.value == 0 and slider_spec.action_zero is not None:
            self.state[slider_spec.control] = 0
            return slider_spec.action_zero
        if prev_value is None:
            # We have no way of knowing where the slider was before...
            self.state[slider_spec.control] = event.value
            return None
        if event.value < prev_value - slider_spec.delta:
            self.state[slider_spec.control] = event.value
            return slider_spec.action_down
        if event.value == 127 or event.value > prev_value + slider_spec.delta:
            self.state[slider_spec.control] = event.value
            return slider_spec.action_up
        return None

    def handle_spinner(self, spinner, event):
        """Handle a spinner change event.
        
        Handles wraparound when the spinner hits the ends of its built-in range.
        """
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
        """Run a command as specified by a Cmd namedtuple."""
        try:
            run(cmd_spec.arg_list)
        except CalledProcessError:
            logging.info("Command '%s' failed.", cmd_spec.arg_list[0])

    def do_action(self, action):
        """Do whatever the supplied Action specifies."""
        if action == BOING:
            self.init_knobs()
            return
        logging.info("Action %s => %s", action.desc, action.keyspec)
        if not action.keyspec:
            return
        self._do_keyspec(action.keyspec, action)

    def _do_keyspec(self, keyspec, action):
        """Actually do the thing specified in the keyspec.
        
        Send some keystroke(s), run a command, or recurse on list elements.

        Note that order is important here, as Cmd() is a namedTUPLE, and thus
        is an instance of `tuple`.
        """
        if isinstance(keyspec, Cmd):
            self.run_command(keyspec)
        elif isinstance(keyspec, list):
            # List of things to do
            for elem in keyspec:
                self._do_keyspec(elem, action)
        elif isinstance(keyspec, tuple):
            self.send_chord(keyspec)
        elif isinstance(keyspec, str):
            self.send_key(keyspec)
        elif isinstance(keyspec, int):
            # Sleep times are specified in milliseconds
            time.sleep(keyspec / 1000)
        else:
            raise Exception(
                f"Unsupported keyspec of type {type(keyspec)} in Action {action}")

    def run(self):
        """The main loop of the program.

        Initialize the Xlib code for sending keystrokes.
        Initialize the MIDI client API.
        Listen to the stream of MIDI events and dispatch the defined actions.
        Exit when the MIDI controller is disconnected.

        The stream of MIDI events looks like this:
        <ControlChangeEvent channel=10 param=2 value=2>
        <ControlChangeEvent channel=10 param=2 value=1>
        <NoteOnEvent channel=10 note=0 velocity=127>
        <NoteOffEvent channel=10 note=0 velocity=0>
        <NoteOnEvent channel=10 note=8 velocity=127>
        https://python-alsa-midi.readthedocs.io/en/latest/api_events.html
        """
        # Get a handle for UInput
        self.uinput = UInput()

        # Connect to the MIDI device.
        # TODO This is kind of cargo-culted from the library's example code.
        # I need to understand this stuff better.
        self.client = SequencerClient(CONTROLLER_DEVICE)
        for port in self.client.list_ports():
            if port.name == CONTROLLER_PORT_NAME:
                logging.error("Another instance already connected to %s",
                              CONTROLLER_DEVICE)
                logging.error("Exiting.")
                sys.exit(1)
        self.port = self.client.create_port(CONTROLLER_PORT_NAME)
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
                self.uinput.close()
                sys.exit(0)
            elif event.type == NoteOnEvent.type:
                # This is a button press; we trigger on the button release event.
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
            # Only some events actually trigger Actions
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
