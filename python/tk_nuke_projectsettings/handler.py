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

import os
import sgtk
import nuke

logger = sgtk.platform.get_logger(__name__)


class NukeProjectSettingsHandler:
    """
    Applies ShotGrid project/shot settings (fps, frame range, format) to
    nuke.root() automatically.

    Data source priority:
    1. Env vars staged by hooks/tk-multi-launchapp/before_app_launch.py
       (NFA_PROJECT_FPS, NFA_SHOT_CUT_IN, NFA_SHOT_CUT_OUT). These are
       already resolved once at launch time, so this is the cheap path
       and avoids a second ShotGrid round trip on every script load.
    2. If the env vars are missing (e.g. artist attached to an already
       running Nuke session, or launched outside the ShotGrid launcher),
       fall back to a live ShotGrid query using the current context.

    Only applies values when the current context is a Shot - on Project
    or other entity contexts this is a no-op, matching how
    tk-multi-setframerange scopes itself to settings.tk-nuke.shot_step.
    """

    def __init__(self):
        self.app = sgtk.platform.current_bundle()

    def _get_fps(self, context):
        env_fps = os.environ.get("NFA_PROJECT_FPS")
        if env_fps:
            try:
                return float(env_fps)
            except ValueError:
                logger.warning("NFA_PROJECT_FPS env var is not numeric: %s", env_fps)

        # Fallback: live query
        project = context.project
        if not project:
            return None
        sg = self.app.shotgun
        result = sg.find_one("Project", [["id", "is", project["id"]]], ["sg_fps"])
        if result and result.get("sg_fps") is not None:
            return float(result["sg_fps"])
        return None

    def _get_frame_range(self, context):
        env_in = os.environ.get("NFA_SHOT_CUT_IN")
        env_out = os.environ.get("NFA_SHOT_CUT_OUT")
        if env_in and env_out:
            try:
                return int(env_in), int(env_out)
            except ValueError:
                logger.warning(
                    "NFA_SHOT_CUT_IN/OUT env vars are not numeric: %s/%s",
                    env_in,
                    env_out,
                )

        # Fallback: live query, only meaningful for a Shot context
        entity = context.entity
        if not entity or entity.get("type") != "Shot":
            return None, None
        sg = self.app.shotgun
        result = sg.find_one(
            "Shot", [["id", "is", entity["id"]]], ["sg_cut_in", "sg_cut_out"]
        )
        if result and result.get("sg_cut_in") is not None and result.get("sg_cut_out") is not None:
            return int(result["sg_cut_in"]), int(result["sg_cut_out"])
        return None, None

    def apply_settings(self):
        """
        Applies fps and frame range to nuke.root(). Called on new-file
        creation and on script load, so both a fresh session and an
        opened .nk pick up current ShotGrid values.
        """
        context = self.app.context

        # Only act on Shot-scoped contexts - matches where this app is
        # registered (settings.tk-nuke.shot_step), but guards against
        # being invoked from a stale context after a context switch.
        if not context.entity or context.entity.get("type") != "Shot":
            logger.debug(
                "tk-nuke-projectsettings: context is not a Shot, skipping"
            )
            return

        root = nuke.root()

        fps = self._get_fps(context)
        if fps is not None:
            if root["fps"].value() != fps:
                root["fps"].setValue(fps)
                logger.info("tk-nuke-projectsettings: set fps to %s", fps)
        else:
            logger.info(
                "tk-nuke-projectsettings: no sg_fps found, leaving fps untouched"
            )

        first_frame, last_frame = self._get_frame_range(context)
        if first_frame is not None and last_frame is not None:
            if (
                root["first_frame"].value() != first_frame
                or root["last_frame"].value() != last_frame
            ):
                root["first_frame"].setValue(first_frame)
                root["last_frame"].setValue(last_frame)
                logger.info(
                    "tk-nuke-projectsettings: set frame range to %s-%s",
                    first_frame,
                    last_frame,
                )
        else:
            logger.info(
                "tk-nuke-projectsettings: no sg_cut_in/sg_cut_out found, "
                "leaving frame range untouched"
            )

    def add_callbacks(self):
        # Run once when a brand new/empty script is created, and again
        # whenever a script is loaded/opened - covers both "new file from
        # ShotGrid" and "open an existing published .nk" workflows.
        nuke.addOnCreate(self.apply_settings, nodeClass="Root")
        nuke.addOnScriptLoad(self.apply_settings, nodeClass="Root")

    def remove_callbacks(self):
        nuke.removeOnCreate(self.apply_settings, nodeClass="Root")
        nuke.removeOnScriptLoad(self.apply_settings, nodeClass="Root")
