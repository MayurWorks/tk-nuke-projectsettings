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

# Name of a hidden root() user knob used purely as an in-session marker
# (never saved meaningfully, just present on the Root object) recording
# that apply_settings() has already run for the current script instance.
# Used by apply_settings_if_new() so a save operation only applies
# settings to a genuinely new/never-configured script, not to a script
# that was already opened or template-generated (and may have had its
# fps/frame range/OCIO deliberately changed by the artist since).
_SETTINGS_APPLIED_KNOB = "sx_projectsettings_applied"

# Prefix used for the Nuke Format name registered for a given plate
# resolution (see _apply_format_from_plate). Includes the dimensions so
# a second shot at the same resolution reuses the existing nuke.Format
# object (nuke.addFormat raises if a format of the same name but
# different dimensions already exists) instead of erroring or
# accumulating duplicate same-named formats across shots in one session.
_FORMAT_NAME_PREFIX = "sx_plate"


class NukeProjectSettingsHandler:
    """
    Applies ShotGrid project/shot settings (fps, frame range, OCIO, the
    ingested plate as a Read node, and the project/full-size format) to
    the current Nuke script automatically.

    Full-size format specifically is ALWAYS derived from the actual
    ingested EXR plate's own width/height (read directly off the file via
    a throwaway Read node - see _read_exr_dimensions()), never from a
    hardcoded resolution. If no plate is ingested yet, or its dimensions
    can't be read, the script's existing format is left untouched rather
    than substituting a default - see apply_settings()'s format-handling
    block.

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
        Resolves the shot's plate root directory - the "Projects/
        {Projectcode}/Plates/{Sequence}/{Shot}" folder that contains one
        p<version> subfolder per Hiero ingest/re-ingest (p001, p002, ...) -
        without needing to know the plate version number in advance.

        Derived entirely from the "shot_plate" path template
        (core/templates/tk-nuke.yml in nfa-shotgun-configuration) via
        template.apply_fields(), rather than hand-joining path segments
        here. This is deliberate: an earlier revision of this method
        built the path by manually os.path.join()-ing "Projects",
        "Plates", sequence, shot onto tk.roots["primary"], duplicating
        the template's own logic in a second place - exactly the kind of
        drift this method is meant to avoid. Now, if the studio's folder
        convention changes again (as it did when {Projectcode} moved
        from "implicit at the project root" to "an explicit folder under
        Artists/Projects"), this method picks up the change automatically
        from the template with no edit needed here.

        IMPORTANT (found 2026-09-16, live on this site): the Sequence/
        Shot field values used to fill in shot_plate are deliberately
        resolved via context.as_template_fields(nuke_shot_work) - the
        WORKING-FILE template - rather than
        context.as_template_fields(shot_plate_template) directly.
        as_template_fields() validates the resolved path against
        Toolkit's path cache, which requires
        tk.create_filesystem_structure() to have already been run for
        that exact folder. Projects/Plates/{Sequence}/{Shot} is NEVER
        created that way on this site - Hiero's tk-hiero-export writes
        EXRs there via a direct file copy (hiero_copy_path), completely
        bypassing Toolkit's folder-creation API, for every shot, always.
        Confirmed by direct inspection: PathCache.get_paths("Shot", ...)
        for a shot with fully-working Artists/ and Publish/ folders
        returned zero registered paths under Projects/Plates/, and
        running tk.create_filesystem_structure() again (Shot-scoped,
        Project-scoped, with and without an engine filter) made no
        difference - Toolkit reports "already up to date" because
        nothing has ever queued a creation event for that branch, not
        because the folder is actually registered. Calling
        as_template_fields(shot_plate_template) directly therefore always
        raises TankError, on every shot, permanently - not a transient
        state fixable by re-running folder creation.
        nuke_shot_work resolves the exact same Sequence/Shot values (it's
        the same Shot, same context) and validates cleanly, because
        Workfiles2's own folder creation IS what put the artist's .nk
        file under Artists/ in the first place. Those field values are
        then applied to shot_plate_template via apply_fields() - pure
        string substitution, which never touches the path cache - so
        the never-created Projects/Plates folder is never validated
        against, only its path string is built.

        shot_plate's own definition ends in "{Shot}.{SEQ}.exr" - a
        FILENAME, not the version-numbered directory hiero actually
        ingests into (shot_plate has no {version}/p{version} segment,
        unlike hiero_copy_path/hiero_render_path in tk-hiero.yml, which do
        insert one - shot_plate was never updated to match what
        tk-hiero-export actually writes). So this method resolves
        shot_plate with a placeholder version/frame and then walks BACK
        UP two path segments (past the filename, past the {Sequence}/
        {Shot} leaf it already has) to recover the true
        "Projects/{Projectcode}/Plates/{Sequence}/{Shot}" root - i.e. it
        takes the directory two levels above what shot_plate resolves to,
        which is exactly the {Shot} folder itself, since shot_plate's
        definition is "{shot_plate_root}/{Shot}.{SEQ}.exr" with no
        intermediate folder between {Shot} and the filename. This still
        does not require knowing the real plate version number, since the
        version-numbered p<version> subfolder is discovered by the
        existing glob-based scan in _resolve_plate_sequence_path(), not by
        this method.
        """
        tk = self.app.sgtk
        try:
            shot_plate_template = tk.templates.get("shot_plate")
            if shot_plate_template is None:
                return None

            # See the IMPORTANT note above: resolve fields via the
            # working-file template (validates cleanly against the path
            # cache) rather than shot_plate_template itself (would raise
            # TankError - Projects/Plates is never Toolkit-created here).
            work_template = tk.templates.get("nuke_shot_work")
            if work_template is None:
                return None
            fields = context.as_template_fields(work_template)
            sequence = fields.get("Sequence")
            shot = fields.get("Shot")
            if not sequence or not shot:
                return None

            # SEQ (a Toolkit "sequence" key, e.g. frame numbers) isn't
            # known yet at this point - fill in a syntactically-valid
            # placeholder purely so the template can resolve to a path
            # string; only the parent directory of that path is used
            # below, so the placeholder's actual value never surfaces.
            fields["SEQ"] = 0
            resolved_file_path = shot_plate_template.apply_fields(fields)

            # resolved_file_path ends in ".../{Shot}/{Shot}.0000.exr" -
            # its immediate parent directory is the {Shot} folder, i.e.
            # exactly the plate root this method returns.
            plate_root = os.path.dirname(resolved_file_path)
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
            if not plate_root:
                logger.info(
                    "tk-nuke-projectsettings: _find_plate_root_dir() "
                    "returned no plate root (shot_plate template did not "
                    "resolve for this context) - no plate to scan"
                )
            elif not os.path.isdir(plate_root):
                logger.info(
                    "tk-nuke-projectsettings: resolved plate root '%s' "
                    "does not exist on disk - has this shot's plate been "
                    "ingested yet?",
                    plate_root,
                )
            else:
                version_dirs = sorted(
                    d for d in glob.glob(os.path.join(plate_root, "p*"))
                    if os.path.isdir(d)
                )
                if not version_dirs:
                    logger.info(
                        "tk-nuke-projectsettings: plate root '%s' exists "
                        "but has no p<version> subfolders (e.g. p001) - "
                        "nothing ingested here yet",
                        plate_root,
                    )
                else:
                    latest_version_dir = version_dirs[-1]
                    # EXRs live under a {fileext} subfolder, e.g. "exr".
                    exr_subdirs = [
                        d for d in glob.glob(os.path.join(latest_version_dir, "*"))
                        if os.path.isdir(d) and glob.glob(os.path.join(d, "*.exr"))
                    ]
                    if not exr_subdirs:
                        logger.info(
                            "tk-nuke-projectsettings: latest version folder "
                            "'%s' has no subfolder containing .exr files - "
                            "checked immediate subfolders only (e.g. "
                            "'exr/'); if EXRs live deeper or under a "
                            "different extension folder, they won't be "
                            "found",
                            latest_version_dir,
                        )
                    else:
                        seq_dir = exr_subdirs[0]

        if not seq_dir or not os.path.isdir(seq_dir):
            return None, None, None

        # Any .exr in the resolved shot-plate directory is treated as part
        # of the ingested sequence.
        frame_files = sorted(glob.glob(os.path.join(seq_dir, "*.exr")))

        if not frame_files:
            logger.info(
                "tk-nuke-projectsettings: sequence directory '%s' resolved "
                "but contains no .exr files",
                seq_dir,
            )
            return None, None, None

        logger.info(
            "tk-nuke-projectsettings: found %d EXR frame(s) for plate in "
            "'%s'",
            len(frame_files),
            seq_dir,
        )

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

    def _read_exr_dimensions(self, printf_path, first_frame, last_frame):
        """
        Reads the actual width/height of the ingested EXR plate sequence
        by creating a throwaway, never-shown Read node pointed at one real
        frame and querying its resolved width()/height() - which reflect
        the file's real display-window dimensions (backed by the EXR
        header's own displayWindow, the same data nuke.toNode(...).
        metadata()'s "input/width"/"input/height" keys expose), not a
        value inferred from the script or hardcoded anywhere in this
        pipeline.

        A dedicated throwaway node (rather than reusing/inspecting the
        real plate_<Shot> Read node this handler creates elsewhere) is
        used deliberately: this method may run before that node exists
        yet (format must be known before or alongside Read-node creation,
        not after), and creating it does not depend on
        _create_or_update_plate_read()'s own existing-node/repath-guard
        logic at all - the two are independent concerns.

        Returns (width, height) as ints, or (None, None) if no frame
        could be read (e.g. missing/corrupt file) - callers must treat
        that as "dimensions unknown" and must NOT substitute a hardcoded
        resolution; see apply_settings()'s handling of this return value.
        """
        if not printf_path or first_frame is None:
            return None, None

        # Resolve one concrete frame path to read - Nuke's Read node can
        # evaluate width()/height() from a single frame without needing
        # the full first/last range set correctly first.
        sample_frame = first_frame
        try:
            sample_path = printf_path.replace(os.sep, "/") % sample_frame
        except (TypeError, ValueError):
            logger.warning(
                "tk-nuke-projectsettings: could not format printf path "
                "'%s' with frame %s to read EXR dimensions",
                printf_path,
                sample_frame,
            )
            return None, None

        if not os.path.isfile(sample_path):
            logger.warning(
                "tk-nuke-projectsettings: sample plate frame '%s' does "
                "not exist on disk, cannot read EXR dimensions",
                sample_path,
            )
            return None, None

        probe_node = None
        try:
            probe_node = nuke.createNode("Read", inpanel=False)
            # Keep the throwaway node out of the node graph the artist
            # sees and out of anything that might process it - it exists
            # purely to let Nuke's own EXR reader tell us the real
            # dimensions, and is deleted immediately after.
            probe_node["file"].setValue(sample_path)
            probe_node["first"].setValue(sample_frame)
            probe_node["last"].setValue(sample_frame)
            width = int(probe_node.width())
            height = int(probe_node.height())
            if width <= 0 or height <= 0:
                logger.warning(
                    "tk-nuke-projectsettings: EXR dimensions read as "
                    "non-positive (%sx%s) from '%s', treating as unknown",
                    width,
                    height,
                    sample_path,
                )
                return None, None
            return width, height
        except Exception:
            logger.warning(
                "tk-nuke-projectsettings: failed to read EXR dimensions "
                "from '%s'",
                sample_path,
                exc_info=True,
            )
            return None, None
        finally:
            if probe_node is not None:
                try:
                    nuke.delete(probe_node)
                except Exception:
                    logger.warning(
                        "tk-nuke-projectsettings: could not remove "
                        "throwaway EXR-probe Read node",
                        exc_info=True,
                    )

    def _apply_format_from_plate(self, root, width, height):
        """
        Sets the Nuke script's project/full-size format (root()["format"])
        to a format matching the given width/height, registering a new
        nuke.Format via nuke.addFormat() if one matching these exact
        dimensions doesn't already exist in this session.

        Deliberately keyed on (width, height) in the format's own name
        (see _FORMAT_NAME_PREFIX) rather than always calling addFormat()
        unconditionally - nuke.addFormat() with a name that already
        exists at a DIFFERENT size raises, and re-adding the same
        name/size repeatedly across every apply_settings() call in a
        session (e.g. on every save) would either error or accumulate
        redundant work for no benefit. A second shot using the same
        plate resolution reuses the same registered format instead of
        creating a duplicate.

        Never called with a hardcoded fallback resolution - if width/
        height are None, the caller (apply_settings) simply does not
        call this method at all and leaves the script's existing format
        untouched; see apply_settings()'s format-handling block and its
        log message for the missing-metadata case.
        """
        format_name = "%s_%dx%d" % (_FORMAT_NAME_PREFIX, width, height)
        try:
            existing = None
            for fmt in nuke.formats():
                if fmt.name() == format_name:
                    existing = fmt
                    break

            if existing is None:
                # TCL format string: "width height pixel_aspect name"
                nuke.addFormat("%d %d 1.0 %s" % (width, height, format_name))

            if root["format"].value().name() != format_name:
                root["format"].setValue(format_name)
                logger.info(
                    "tk-nuke-projectsettings: set project format to %s "
                    "(%dx%d, from ingested plate EXR metadata)",
                    format_name,
                    width,
                    height,
                )
        except Exception:
            logger.warning(
                "tk-nuke-projectsettings: could not set project format "
                "to %dx%d from plate metadata",
                width,
                height,
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

    def _template_app_owns_this_load(self):
        """
        True if tk-nuke-template's own addOnScriptLoad callback is about
        to run generate_template() for this same script-load event (i.e.
        a brand-new/empty script, or one containing its
        createTemplatePlaceholder node) and will explicitly call
        apply_settings() itself once that finishes.

        Only consulted by this handler's own addOnCreate/addOnScriptLoad
        callbacks (see add_callbacks), to defer to tk-nuke-template's
        explicit call instead of running apply_settings() a second,
        redundant time on the same event. Deliberately NOT consulted
        when tk-nuke-template calls apply_settings() directly - at that
        point in generate_template() the createTemplatePlaceholder node
        is often still present (it's intentionally kept, not deleted, by
        that function's node cleanup pass), so this check would
        otherwise cause the explicit call to defer to itself and
        silently do nothing on every new-file-from-template.
        """
        try:
            nodes = nuke.allNodes()
            if len(nodes) == 0:
                return True
            for node in nuke.allNodes("ModifyMetaData"):
                if node.name() == "createTemplatePlaceholder":
                    return True
        except Exception:
            logger.warning(
                "tk-nuke-projectsettings: could not determine whether "
                "tk-nuke-template owns this load, proceeding as normal",
                exc_info=True,
            )
        return False

    def apply_settings(self, _skip_if_template_owns_load=False):
        """
        Applies OCIO, fps, frame range, the ingested-plate Read node, and
        the project/full-size format (derived from that plate's actual
        EXR dimensions) to nuke.root(). Called on genuine script open (an
        existing .nk being loaded, via this app's own callbacks) and,
        explicitly, by tk-nuke-template once it has finished building a
        brand-new script from a template.

        _skip_if_template_owns_load is only ever passed True from this
        handler's own add_callbacks() registrations - never by
        tk-nuke-template's explicit call - so the new-file-from-template
        case is handled exactly once, by that explicit call, regardless
        of Nuke callback registration order between the two apps. See
        _template_app_owns_this_load() for why the two call sites can't
        share the same check.
        """
        if _skip_if_template_owns_load and self._template_app_owns_this_load():
            logger.debug(
                "tk-nuke-projectsettings: new-file-from-template detected, "
                "deferring to tk-nuke-template's explicit call after "
                "generate_template() completes"
            )
            return

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

        # --- Full-size project format: derived from the ingested plate's
        # own EXR dimensions, never hardcoded. Uses disk_first (the first
        # frame actually found on disk for the ingested sequence) rather
        # than first_frame/last_frame above, since those may have come
        # from sg_cut_in/sg_cut_out instead of the disk scan and are not
        # guaranteed to be a frame that actually exists in this
        # particular plate sequence. ---
        if printf_path and disk_first is not None:
            width, height = self._read_exr_dimensions(
                printf_path, disk_first, disk_last
            )
            if width is not None and height is not None:
                self._apply_format_from_plate(root, width, height)
            else:
                logger.info(
                    "tk-nuke-projectsettings: could not read EXR "
                    "dimensions from the ingested plate, leaving project "
                    "format untouched (never falling back to a "
                    "hardcoded resolution)"
                )
        else:
            logger.info(
                "tk-nuke-projectsettings: no ingested plate found for "
                "this shot yet, leaving project format untouched (never "
                "falling back to a hardcoded resolution)"
            )

        self._mark_settings_applied(root)

    def _mark_settings_applied(self, root):
        """
        Records on root() that apply_settings() has run for this script
        instance, via a hidden, non-persisted user knob. Read by
        _settings_already_applied() / apply_settings_if_new().
        """
        try:
            if _SETTINGS_APPLIED_KNOB not in root.knobs():
                knob = nuke.Boolean_Knob(_SETTINGS_APPLIED_KNOB)
                knob.setFlag(nuke.INVISIBLE)
                # Not saved to disk - this is purely an in-session marker
                # so a later Save/Save As of THIS session doesn't
                # re-trigger apply_settings_if_new(), while a fresh
                # process opening the saved file has no marker and is
                # correctly treated as needing settings again if nothing
                # else (open/new callbacks) already applied them.
                knob.setFlag(nuke.DO_NOT_WRITE)
                root.addKnob(knob)
            root[_SETTINGS_APPLIED_KNOB].setValue(True)
        except Exception:
            logger.warning(
                "tk-nuke-projectsettings: could not set in-session "
                "settings-applied marker",
                exc_info=True,
            )

    def _settings_already_applied(self, root):
        try:
            return (
                _SETTINGS_APPLIED_KNOB in root.knobs()
                and root[_SETTINGS_APPLIED_KNOB].value()
            )
        except Exception:
            return False

    def apply_settings_if_new(self):
        """
        Applies settings only if this script instance hasn't already had
        them applied this session (via open/new-file callbacks or a
        previous call to this method). Intended for the save/save_as
        scene-operation hook: a script saved for the first time without
        ever going through the normal open/new triggers (e.g. built up
        by hand, or any other path that bypassed those callbacks) still
        gets fps/frame range/OCIO/plate Read set before it hits disk.
        Deliberately a no-op for a script that was already
        opened/template-generated in this session, so it never
        overwrites values an artist has since changed on purpose - see
        _mark_settings_applied().
        """
        root = nuke.root()
        if self._settings_already_applied(root):
            logger.debug(
                "tk-nuke-projectsettings: settings already applied this "
                "session, skipping on save"
            )
            return
        logger.info(
            "tk-nuke-projectsettings: script has no settings-applied "
            "marker, applying settings before save"
        )
        self.apply_settings()

    def _apply_settings_from_callback(self):
        """
        nuke.addOnCreate/addOnScriptLoad invoke their callback with no
        arguments, so this thin wrapper is what actually gets registered
        - it's what lets these two callback-driven call sites pass
        _skip_if_template_owns_load=True, while tk-nuke-template's direct
        call to self.app.handler.apply_settings() (see
        tk-nuke-template's generate_template()) goes straight to
        apply_settings() itself and is unaffected by this flag.
        """
        self.apply_settings(_skip_if_template_owns_load=True)

    def add_callbacks(self):
        # Run once when a brand new/empty script is created, and again
        # whenever a script is loaded/opened - covers both a genuine
        # "open an existing published .nk" workflow and a fallback for
        # new-file creation if tk-nuke-projectsettings is ever used
        # without tk-nuke-template in the environment. When
        # tk-nuke-template IS present, its own explicit call after
        # generate_template() (not this callback) is what actually
        # handles the new-file-from-template case - see
        # _template_app_owns_this_load().
        nuke.addOnCreate(self._apply_settings_from_callback, nodeClass="Root")
        nuke.addOnScriptLoad(self._apply_settings_from_callback, nodeClass="Root")

    def remove_callbacks(self):
        nuke.removeOnCreate(self._apply_settings_from_callback, nodeClass="Root")
        nuke.removeOnScriptLoad(self._apply_settings_from_callback, nodeClass="Root")
