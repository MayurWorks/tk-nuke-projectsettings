"""
Resolves the per-shot pipeline settings from ShotGrid.

Precedence:  Shot value  >  Project value  >  unset (caller decides).

The same API field names exist on Project and Shot. Project values are the
defaults (entered in the project creator); the shot_defaults event plugin
copies them onto new shots, and anyone can override them per shot by simply
filling the Shot field. An empty Shot field means "inherit from project", so
shots that were never stamped still resolve correctly.

KEEP IN SYNC with SETTING_FIELDS in the event plugin (shot_defaults.py) and
FIELDS_TO_CREATE in the project creator (ensure_schema.py).
"""

import logging

logger = logging.getLogger(__name__)

SETTING_FIELDS = (
    "sg_frame_rate",
    "sg_format_width",
    "sg_format_height",
    "sg_color_pipeline",
)

# Integer field that predates sg_frame_rate; still read on Project so
# projects created before the new fields existed keep working.
_LEGACY_PROJECT_FPS = "sg_fps"

SOURCE_SHOT = "shot"
SOURCE_PROJECT = "project"


def is_set(value):
    """None, '' and 0 all mean 'not set' (0 fps / 0 px is never valid)."""
    return value is not None and value != "" and value != 0


def _find(sg, entity_type, entity, fields):
    return sg.find_one(entity_type, [["id", "is", entity["id"]]], list(fields)) or {}


def resolve_settings(sg, project, shot=None):
    """
    Returns {field: (value, source)} for every setting that has a value,
    where source is "shot" or "project". Fields with no value anywhere are
    absent from the result.

    Raises only if ShotGrid itself is unreachable; a site that does not have
    the new fields yet (creator not run) degrades to the legacy fields.
    """
    project_row, shot_row = {}, {}

    try:
        if project:
            project_row = _find(
                sg, "Project", project, SETTING_FIELDS + (_LEGACY_PROJECT_FPS,)
            )
        if shot:
            shot_row = _find(sg, "Shot", shot, SETTING_FIELDS)
    except Exception:
        # Most likely a field that doesn't exist on this site yet. Retry with
        # what the app used before this feature so nothing regresses; a real
        # outage raises again here and propagates to the caller.
        logger.warning(
            "tk-nuke-projectsettings: settings query failed, retrying with "
            "legacy fields only",
            exc_info=True,
        )
        project_row = (
            _find(sg, "Project", project, ("sg_color_pipeline", _LEGACY_PROJECT_FPS))
            if project
            else {}
        )
        shot_row = {}

    resolved = {}
    for field in SETTING_FIELDS:
        if is_set(shot_row.get(field)):
            resolved[field] = (shot_row[field], SOURCE_SHOT)
        elif is_set(project_row.get(field)):
            resolved[field] = (project_row[field], SOURCE_PROJECT)

    if "sg_frame_rate" not in resolved and is_set(project_row.get(_LEGACY_PROJECT_FPS)):
        resolved["sg_frame_rate"] = (
            float(project_row[_LEGACY_PROJECT_FPS]),
            SOURCE_PROJECT,
        )

    return resolved


def resolved_format(resolved):
    """(width, height, source) if both are set, else None."""
    width = resolved.get("sg_format_width")
    height = resolved.get("sg_format_height")
    if width and height:
        source = SOURCE_SHOT if SOURCE_SHOT in (width[1], height[1]) else SOURCE_PROJECT
        return int(width[0]), int(height[0]), source
    return None
