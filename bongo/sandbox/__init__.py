"""Modules imported from GitHub or a .zip (Settings > Modules > Import) run here, never in the Matrix's own
process: each in a child process of its own, as a user of its own, that sees only what it needs (its own
folder, a folder it may write to, Python and the fonts) and reaches the internet only through the gate
(``gate.py``), which lets it through to the sites its ``bongo-module.json`` lists and nowhere else.

    wire.py     the messages between the Matrix and a child (JSON, plus a frame's pixels)
    host.py     the child: loads the module and draws its frames when asked
    jail.py     starts a child: its own user, its own view of the files, no network but the gate
    gate.py     the only way out for a child: the sites its module lists, never the home network
    remote.py   the Matrix's side: a Module whose styles ask the child for each frame
    background.py  the Matrix's side of a module's background task, for one that asked for it (bongo/abilities.py)
"""
