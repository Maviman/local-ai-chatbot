"""fx/page08 - the sections of Settings page 08 "Sound & motion", one module each (B10: motion.py).

fx/sound_motion_page.discover() imports every module here (names starting with "_" are skipped) the first time the
page is built; each module ends with its one-line registration, e.g.

    register_section("motion", 300, MotionSection)

so a phase adds its section as a NEW file here and edits nothing else (B11: the CRT rows, order 310; B13: the
sound sections, orders 100 / 200; B14: the Signal acquired / Context limit alert lamps, opt_in_cues.py, order 210).
The registry API is in fx/sound_motion_page.py's docstring.
"""
