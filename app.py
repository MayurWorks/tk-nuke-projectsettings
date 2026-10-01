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

    def get_pipeline_settings(self, context):
        """
        Public API for other apps/hooks: resolves the same Shot > Project
        pipeline settings (sg_frame_rate, sg_format_width, sg_format_height,
        sg_color_pipeline) this app applies to nuke.root() on launch - see
        pipeline_settings.py for the precedence rules and the legacy sg_fps
        fallback.

        Added so a hook that only knows the context (for example a publish2
        validation plugin, which has no reason to duplicate this app's
        ShotGrid queries or its Shot > Project logic) can ask "what should
        this shot's settings be right now" and get the same answer this app
        itself would use.

        Returns {field: (value, source)}, source being "shot" or "project" -
        same shape as pipeline_settings.resolve_settings(). Never raises: on
        any failure (ShotGrid unreachable, fields not on this site) returns
        {} rather than surfacing an exception to a caller that may not be
        expecting one.
        """
        try:
            return self.handler._resolve_settings(context)
        except Exception:
            self.log_warning(
                "get_pipeline_settings: could not resolve settings for %s"
                % context
            )
            return {}
