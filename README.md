# Midimapper

A simple utility for generating keyboard shortcuts (and other more esoteric
events) from a MIDI device.

This was originally developed, and is currently configured, to drive the
[DigiKam](https://www.digikam.org/) photo management system, but it could be
used for pretty much anything.  I use it with a [Behringer X-Touch
Mini](https://www.behringer.com/product.html?modelCode=0808-AAF), but it will
(probably) work with other MIDI devices.

Thanks to my pal [Joel Holveck](mailto:joelh@piquan.org) for the basic Xlib
code, which I have since mangled into near-unrecognizability.

# Requirements

Developed with Python 3.8, [python-alsa-midi 1.0.1](https://python-alsa-midi.readthedocs.io/en/latest/),
and [xlib 0.21](https://pypi.org/project/xlib/).

# License

Released under the GPL v3 or later, see the LICENSE.txt file