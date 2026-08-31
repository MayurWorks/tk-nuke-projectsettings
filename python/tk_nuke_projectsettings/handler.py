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
import re
import glob
import sgtk
import nuke

logger = sgtk.platform.get_logger(__name__)

# Nuke 14+ ships ACES 1.2 as a built-in OCIO config choice - no config file
# on disk is needed, just these two root() knob values.
OCIO_COLOR_MANAGEMENT = "OCIO"
OCIO_CONFIG_NAME = "aces_1.2"

# PublishedFile.published_file_type name registered by tk-hiero-export for
# the copied/ingested plate sequence. On this site only two
# PublishedFileTypes exist at all: "Nuke Script" and "Hiero Plate" -
# and "Hiero Plate" is currently only ever used for the reference .mov
# (hiero_plate_path), never the .exr copy/render sequence
# (hiero_copy_path/hiero_render_path) - confirmed via a site-wide query
# turning up zero .exr PublishedFile entries. This is left set to
# "Hiero Plate" so the PublishedFile lookup starts working the moment
# that registration gap in tk-hiero-export's config is fixed, but until
# then _find_latest_plate_publish() will always return None and
# _resolve_plate_sequence_path()'s directory-scan fallback (which does
# NOT require a PublishedFile to exist) is what actually finds the
# plate. See _resolve_plate_sequence_path's docstring for the disk-scan
# path.
PLATE_PUBLISH_TYPES = ["Hiero Plate"]


class NukeProjectSettingsHandler:
    """
    Applies ShotGrid project/shot settings (fps, frame range, OCIO, and the
    ingested plate as a Read node) to the current Nuke script automatically.

    Data source priority for fps / frame range:
    1. A live ShotGrid query against the current context. This is the
       source of truth - it's what makes the values correct even after
       an in-session context switch (tk-multi-workfiles2 can change
       context without restarting the engine), which env vars staged
       once at process launch cannot reflect.
    2. If the live query fails (e.g. transient ShotGrid connectivity
       issue), fall back to the env vars staged by
       hooks/tk-multi-launchapp/before_app_launch.py (NFA_PROJECT_FPS,
       NFA_SHOT_CUT_IN, NFA_SHOT_CUT_OUT). These only reflect the
       context at launch time, so they are a degraded fallback, not
       the primary source.
    3. For frame range specifically, if the Shot has no sg_cut_in/
       sg_cut_out set at all (neither live nor via env var), fall back
       further to scanning the actual ingested EXR sequence on disk
       (the hiero_copy_path/hiero_render_path plate location) for its
       real first/last frame. This covers shots ingested before cut
       fields were populated in ShotGrid.

    Only applies values when the current context is a Shot - on Project
    or other entity contexts this is a no-op, matching how
    tk-multi-setframerange scopes itself to settings.tk-nuke.shot_step.
    """

    def __init__(self):
        self.app = sgtk.platform.current_bundle()

    def _get_fps(self, context):
        project = context.project
        if project:
            try:
                sg = self.app.shotgun
                result = sg.find_one(
                    "Project", [["id", "is", project["id"]]], ["sg_fps"]
                )
                if result and result.get("sg_fps") is not None:
                    return float(result["sg_fps"])
                return None
            except Exception:
                logger.warning(
                    "tk-nuke-projectsettings: live sg_fps query failed, "
                    "falling back to NFA_PROJECT_FPS env var",
                    exc_info=True,
                )

        # Fallback: env var staged at launch time (may be stale after an
        # in-session context switch, or absent if ShotGrid was reachable
        # but the project has no sg_fps set - only used if the query
        # above didn't run or raised).
        env_fps = os.environ.get("NFA_PROJECT_FPS")
        if env_fps:
            try:
                return float(env_fps)
            except ValueError:
                logger.warning("NFA_PROJECT_FPS env var is not numeric: %s", env_fps)
        return None

    def _get_frame_range(self, context):
        entity = context.entity
        if entity and entity.get("type") == "Shot":
            try:
                sg = self.app.shotgun
                result = sg.find_one(
                    "Shot",
                    [["id", "is", entity["id"]]],
                    ["sg_cut_in", "sg_cut_out"],
                )
                if (
                    result
                    and result.get("sg_cut_in") is not None
                    and result.get("sg_cut_out") is not None
                ):
                    return int(result["sg_cut_in"]), int(result["sg_cut_out"])
                return None, None
            except Exception:
                logger.warning(
                    "tk-nuke-projectsettings: live sg_cut_in/out query failed, "
                    "falling back to NFA_SHOT_CUT_IN/OUT env vars",
                    exc_info=True,
                )

        # Fallback: env vars staged at launch time (may be stale after an
        # in-session context switch - only used if the query above didn't
        # run because context.entity isn't a Shot, or raised).
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
        return None, None

    def _find_latest_plate_publish(self, context):
        """
        Looks up the most recent PublishedFile for this Shot matching the
        ingested-plate publish types (see PLATE_PUBLISH_TYPES) whose path
        is an .exr - guards against "Hiero Plate" also covering the
        reference .mov (hiero_plate_path), which is registered today but
        is not the frame sequence a Read node should point at. Returns
        the PublishedFile dict, or None if nothing matching is published
        yet (which, as of this site's current tk-hiero-export config, is
        always - see PLATE_PUBLISH_TYPES comment).
        """
        entity = context.entity
        if not entity or entity.get("type") != "Shot":
            return None

        try:
            sg = self.app.shotgun
            filters = [
                ["entity", "is", entity],
                ["published_file_type.PublishedFileType.code", "in", PLATE_PUBLISH_TYPES],
            ]
            fields = ["path", "path_cache", "version_number", "created_at", "code"]
            order = [{"field_name": "version_number", "direction": "desc"}]
            results = sg.find(
                "PublishedFile", filters, fields, order=order
            )
            for result in results:
                path = result.get("path")
                local_path = path.get("local_path") if path else None
                if local_path and local_path.lower().endswith(".exr"):
                    return result
            return None
        except Exception:
            logger.warning(
                "tk-nuke-projectsettings: PublishedFile lookup for plate failed",
                exc_info=True,
            )
            return None

    def _find_plate_root_dir(self, context):
        """
        Resolves the shot's plate root directory - the "Projects/Plates/
        {Sequence}/{Shot}" folder that contains one p<version> subfolder
        per Hiero ingest/re-ingest (p001, p002, ...) - without needing to
        know the plate version number in advance.

        Built directly from the primary storage root plus the resolved
        {Sequence}/{Shot} context fields, mirroring how common.yml composes
        shot_root/asset_root (Artists/{Sequence}/{Shot}/{Step}) and
        tk-nuke.yml's shot_plate (Projects/Plates/{Sequence}/{Shot}/...) -
        rather than walking up from a Nuke work-area path by directory-
        level count, which is brittle against template changes (e.g.
        shot_work_area_nuke resolves 6 levels deep:
        Artists/{Sequence}/{Shot}/{Step}/Nuke/{Step}).

        shot_plate itself (core/templates/tk-nuke.yml) is NOT used here
        even though it looks like the obvious template - it's a stale,
        non-versioned definition that was never updated to match what
        tk-hiero-export actually writes (see hiero_copy_path/
        hiero_render_path in core/templates/tk-hiero.yml, which insert a
        p{version} folder that shot_plate omits), so resolving through it
        would silently point at a directory that never receives ingested
        plates.
        """
        tk = self.app.sgtk
        try:
            primary_root = tk.roots.get("primary")
            if not primary_root:
                return None

            # tank_name (the project-root folder name, e.g. "STRM") is not
            # a template field - it's resolved via the schema/roots, same
            # as any other template path. Use any shot-level template to
            # pull the correctly-resolved {Sequence}/{Shot} field values
            # for this context, then compose the Projects/Plates path
            # ourselves rather than depend on a template that requires
            # {version}/{fileext} we don't know yet.
            shot_root_template = tk.templates.get("shot_work_area_nuke")
            if shot_root_template is None:
                return None
            fields = context.as_template_fields(shot_root_template)
            sequence = fields.get("Sequence")
            shot = fields.get("Shot")
            if not sequence or not shot:
                return None

            # tank_name-named project folder under primary root - use
            # context to resolve it via any already-working template
            # rather than hardcoding; shot_work_area_nuke's resolved path
            # already starts with "<primary_root>/<tank_name>/Artists/...",
            # so derive the project-root folder from it directly.
            resolved_work_area = shot_root_template.apply_fields(fields)
            rel_path = os.path.relpath(resolved_work_area, primary_root)
            project_folder = rel_path.split(os.sep)[0]

            plate_root = os.path.join(
                primary_root, project_folder, "Projects", "Plates", sequence, shot
            )
            return plate_root
        except Exception:
            logger.warning(
                "tk-nuke-projectsettings: could not resolve plate root "
                "directory for shot",
                exc_info=True,
            )
            return None

    def _resolve_plate_sequence_path(self, context, publish=None):
        """
        Resolves the on-disk EXR sequence path for the current shot's
        ingested plate, as a Nuke-style %04d printf path suitable for a
        Read node. Prefers the resolved PublishedFile path when given
        (once tk-hiero-export's copy-path registration is wired up on
        this site); otherwise falls back to locating the shot's plate
        root directory (see _find_plate_root_dir), picking the
        highest-numbered p<version> subfolder, and scanning its {fileext}
        subfolder (exr) for the actual frame sequence on disk - this is
        the path currently in effect on this site, since PublishedFile
        registration for the copied EXR sequence isn't happening yet
        (confirmed: only "Hiero Plate" reference .mov entries exist,
        never the .exr copy).

        Returns (printf_path, first_frame, last_frame) or (None, None, None)
        if no sequence could be found on disk.
        """
        seq_dir = None

        if publish and publish.get("path"):
            local_path = publish["path"].get("local_path")
            if local_path:
                candidate_dir = os.path.dirname(local_path)
                # Only use the publish's directory if it actually contains
                # EXRs - a "Hiero Plate" PublishedFile may point at the
                # reference .mov instead (ref/ subfolder), which is not
                # the frame sequence we want for a Read node.
                if glob.glob(os.path.join(candidate_dir, "*.exr")):
                    seq_dir = candidate_dir

        if seq_dir is None:
            plate_root = self._find_plate_root_dir(context)
            if plate_root and os.path.isdir(plate_root):
                version_dirs = sorted(
                    d for d in glob.glob(os.path.join(plate_root, "p*"))
                    if os.path.isdir(d)
                )
                if version_dirs:
                    latest_version_dir = version_dirs[-1]
                    # EXRs live under a {fileext} subfolder, e.g. "exr".
                    exr_subdirs = [
                        d for d in glob.glob(os.path.join(latest_version_dir, "*"))
                        if os.path.isdir(d) and glob.glob(os.path.join(d, "*.exr"))
                    ]
                    if exr_subdirs:
                        seq_dir = exr_subdirs[0]

        if not seq_dir or not os.path.isdir(seq_dir):
            return None, None, None

        # Any .exr in the resolved shot-plate directory is treated as part
        # of the ingested sequence.
        frame_files = sorted(glob.glob(os.path.join(seq_dir, "*.exr")))

        if not frame_files:
            return None, None, None

        frame_numbers = []
        frame_re = re.compile(r"\.(\d+)\.exr$", re.IGNORECASE)
        for f in frame_files:
            m = frame_re.search(f)
            if m:
                frame_numbers.append(int(m.group(1)))

        if not frame_numbers:
            return None, None, None

        first_frame = min(frame_numbers)
        last_frame = max(frame_numbers)

        # Build a Nuke-style printf path from the first matched file,
        # replacing its frame-number run with the correct %0Nd padding.
        sample = frame_files[0]
        m = frame_re.search(sample)
        padding = len(m.group(1))
        printf_path = frame_re.sub(".%%0%dd.exr" % padding, sample)

        return printf_path, first_frame, last_frame

    def _apply_ocio(self, root):
        """
        Sets Nuke's built-in ACES 1.2 OCIO config via Project Settings
        knobs. This is the same as an artist manually setting Color
        Management: OCIO and OCIO Config: aces_1.2 in the dropdown -
        no external .ocio file is used, Nuke 14+ ships this config
        internally.
        """
        try:
            if root["colorManagement"].value() != OCIO_COLOR_MANAGEMENT:
                root["colorManagement"].setValue(OCIO_COLOR_MANAGEMENT)
            if root["OCIO_config"].value() != OCIO_CONFIG_NAME:
                root["OCIO_config"].setValue(OCIO_CONFIG_NAME)
            logger.info(
                "tk-nuke-projectsettings: set color management to OCIO / %s",
                OCIO_CONFIG_NAME,
            )
        except (KeyError, ValueError):
            # KeyError: knob name not present on this root() (older/newer
            # Nuke version with different knob names). ValueError: this
            # Nuke build's OCIO_config dropdown doesn't include aces_1.2.
            # Either way, log and move on rather than breaking script
            # creation over a settings knob.
            logger.warning(
                "tk-nuke-projectsettings: could not set colorManagement/"
                "OCIO_config to OCIO/aces_1.2 - check Nuke version",
                exc_info=True,
            )

    def _create_or_update_plate_read(self, context, printf_path, first_frame, last_frame):
        """
        Creates a Read node for the ingested plate sequence if one doesn't
        already exist for this shot (identified by node name
        "plate_<Shot>"). On later script loads, only refreshes an existing
        node's path/range if it's still pointed at the same plate
        directory this handler set it to - if an artist has repathed it
        (e.g. to a newer manual version, or somewhere else entirely) their
        edit is left alone rather than silently overwritten.
        """
        if not printf_path:
            return

        node_name = "plate_%s" % context.entity["name"]
        existing = nuke.toNode(node_name)
        target_dir = os.path.dirname(printf_path.replace(os.sep, "/"))

        if existing is not None and existing.Class() == "Read":
            current_dir = os.path.dirname(
                existing["file"].value().replace(os.sep, "/")
            )
            if current_dir != target_dir:
                logger.info(
                    "tk-nuke-projectsettings: Read node '%s' already "
                    "points elsewhere (%s), leaving it as-is",
                    node_name,
                    current_dir,
                )
                return
            read_node = existing
        else:
            read_node = nuke.createNode("Read", inpanel=False)
            read_node.setName(node_name)

        read_node["file"].setValue(printf_path.replace(os.sep, "/"))
        if first_frame is not None and last_frame is not None:
            read_node["first"].setValue(first_frame)
            read_node["last"].setValue(last_frame)
            read_node["origfirst"].setValue(first_frame)
            read_node["origlast"].setValue(last_frame)

        logger.info(
            "tk-nuke-projectsettings: set Read node '%s' to %s (%s-%s)",
            node_name,
            printf_path,
            first_frame,
            last_frame,
        )

    def apply_settings(self):
        """
        Applies fps and frame range to nuke.root(). Called on new-file
        creation and on script load, so both a fresh session and an
        opened .nk pick up current ShotGrid values.
        """
        # Read the context fresh from the current engine rather than
        # self.app.context - the latter is captured once when the app
        # bundle is constructed and is not guaranteed to reflect a later
        # in-session context switch (e.g. tk-multi-workfiles2 changing
        # context without a full engine restart).
        engine = sgtk.platform.current_engine()
        context = engine.context

        # Only act on Shot-scoped contexts - matches where this app is
        # registered (settings.tk-nuke.shot_step), but guards against
        # being invoked from a stale context after a context switch.
        if not context.entity or context.entity.get("type") != "Shot":
            logger.debug(
                "tk-nuke-projectsettings: context is not a Shot, skipping"
            )
            return

        root = nuke.root()

        # --- Color management: ACES 1.2 via Nuke's built-in OCIO config ---
        self._apply_ocio(root)

        # --- FPS ---
        fps = self._get_fps(context)
        if fps is not None:
            if root["fps"].value() != fps:
                root["fps"].setValue(fps)
                logger.info("tk-nuke-projectsettings: set fps to %s", fps)
        else:
            logger.info(
                "tk-nuke-projectsettings: no sg_fps found, leaving fps untouched"
            )

        # --- Plate lookup (used for both the Read node and, if needed,
        # as the disk-scan fallback source for frame range) ---
        publish = self._find_latest_plate_publish(context)
        printf_path, disk_first, disk_last = self._resolve_plate_sequence_path(
            context, publish=publish
        )

        # --- Frame range: ShotGrid cut_in/cut_out first, ingested-plate
        # disk scan as fallback when SG has no cut fields set ---
        first_frame, last_frame = self._get_frame_range(context)
        source = "sg_cut_in/sg_cut_out"
        if first_frame is None or last_frame is None:
            if disk_first is not None and disk_last is not None:
                first_frame, last_frame = disk_first, disk_last
                source = "ingested plate sequence on disk"

        if first_frame is not None and last_frame is not None:
            if (
                root["first_frame"].value() != first_frame
                or root["last_frame"].value() != last_frame
            ):
                root["first_frame"].setValue(first_frame)
                root["last_frame"].setValue(last_frame)
                logger.info(
                    "tk-nuke-projectsettings: set frame range to %s-%s (source: %s)",
                    first_frame,
                    last_frame,
                    source,
                )
        else:
            logger.info(
                "tk-nuke-projectsettings: no sg_cut_in/sg_cut_out and no "
                "ingested plate found on disk, leaving frame range untouched"
            )

        # --- Auto-pickup: Read node from the ingested plate sequence ---
        if printf_path:
            self._create_or_update_plate_read(
                context, printf_path, disk_first, disk_last
            )
        else:
            logger.info(
                "tk-nuke-projectsettings: no ingested plate found for this "
                "shot yet, skipping Read node creation"
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