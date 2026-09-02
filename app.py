# MIT License
#
# Copyright (c) 2026 SlateX
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
tk-nuke-projectsettings

Applies ShotGrid Project/Shot field values (fps, frame range, format) to
nuke.root() automatically, so artists don't have to set them by hand.

Modeled directly on nfa-vfxim/tk-nuke-template's app.py structure: a thin
Application subclass that hands off to a handler class registering Nuke
callbacks in init_app/destroy_app.
"""

from sgtk.platform import Application


class NukeProjectSettings(Application):
    """
    The app entry point. Registers Nuke callbacks that apply ShotGrid
    project/shot settings to the current script.
    """

    def init_app(self):
        """
        Initialisation for tk-nuke-projectsettings
        """
        self.tk_nuke_projectsettings = self.import_module("tk_nuke_projectsettings")
        self.handler = self.tk_nuke_projectsettings.NukeProjectSettingsHandler()

        # Add callbacks
        self.handler.add_callbacks()

    def destroy_app(self):
        """
        Called when the app is unloaded/destroyed
        """
        self.log_debug("Destroying tk-nuke-projectsettings app")

        # Remove any callbacks that were registered by the handler
        self.handler.remove_callbacks()
