"""
Unit tests for tk_nuke_projectsettings.handler.NukeProjectSettingsHandler.

Runs WITHOUT a real Nuke or ShotGrid Toolkit install by installing minimal
fake `nuke` and `sgtk` modules into sys.modules before importing the
handler (the standard technique for unit-testing Toolkit/Nuke app code in
CI). This intentionally only fakes the surface area handler.py actually
touches - it is not a general-purpose Nuke/sgtk stub.

Run with: python3 -m pytest tests/test_handler.py -v
"""
import os
import sys
import types
import tempfile
import shutil
import glob as glob_module

import pytest


# --------------------------------------------------------------------------
# Fake `nuke` module
# --------------------------------------------------------------------------

class FakeKnob:
    def __init__(self, initial=None):
        self._value = initial
        self._flags = set()

    def value(self):
        return self._value

    def setValue(self, v):
        self._value = v

    def setFlag(self, flag):
        self._flags.add(flag)


class FakeFormatKnob(FakeKnob):
    """
    root()["format"] holds a Format object, not a plain value -
    setValue() in real Nuke accepts a format NAME (string) and resolves
    it against the registered nuke.formats() list; value() then returns
    the resolved Format object. formats_lookup is a callable returning
    the current list of registered FakeFormat objects (kept live so
    formats added via nuke.addFormat() after this knob was constructed
    are still found).
    """
    def __init__(self, initial, formats_lookup):
        super().__init__(initial=initial)
        self._formats_lookup = formats_lookup

    def setValue(self, name_or_format):
        if isinstance(name_or_format, str):
            for fmt in self._formats_lookup():
                if fmt.name() == name_or_format:
                    self._value = fmt
                    return
            raise ValueError("no such format: %s" % name_or_format)
        self._value = name_or_format


class FakeFormat:
    def __init__(self, width, height, pixel_aspect, name):
        self._width = width
        self._height = height
        self._pixel_aspect = pixel_aspect
        self._name = name

    def name(self):
        return self._name

    def width(self):
        return self._width

    def height(self):
        return self._height


class FakeNode:
    """A generic fake Nuke node (Root, Read, Boolean_Knob holder, etc.)."""
    _all_nodes_registry = []

    def __init__(self, node_class="NoOp", name=None):
        self._class = node_class
        self._name = name or ("%s%d" % (node_class, len(FakeNode._all_nodes_registry) + 1))
        self._knobs = {}
        self._deleted = False
        FakeNode._all_nodes_registry.append(self)

    def Class(self):
        return self._class

    def name(self):
        return self._name

    def setName(self, name):
        self._name = name

    def knobs(self):
        return self._knobs

    def addKnob(self, knob):
        # knob objects created via nuke.Boolean_Knob(name) below carry
        # their own name; store under that name.
        self._knobs[knob._name] = knob

    def __getitem__(self, key):
        if key not in self._knobs:
            self._knobs[key] = FakeKnob()
        return self._knobs[key]

    def __setitem__(self, key, knob):
        self._knobs[key] = knob

    def __contains__(self, key):
        return key in self._knobs

    # Read-node-ish helpers used by the EXR-dimension probe
    def width(self):
        return self._fake_width

    def height(self):
        return self._fake_height


class FakeBooleanKnob(FakeKnob):
    def __init__(self, name):
        super().__init__(initial=False)
        self._name = name


def make_fake_nuke_module(fake_disk):
    """
    fake_disk: dict mapping a sample frame path -> (width, height), used
    by the fake Read node to answer .width()/.height() as if it had
    actually read the EXR header - this is the seam that lets tests
    control "what the EXR metadata says" without needing real EXR files.
    """
    fake_nuke = types.ModuleType("nuke")

    fake_nuke.INVISIBLE = "INVISIBLE"
    fake_nuke.DO_NOT_WRITE = "DO_NOT_WRITE"

    state = {
        "root": FakeNode(node_class="Root", name="root"),
        "nodes": [],
        "formats": [],
    }
    # Root always has fps/first_frame/last_frame/colorManagement/
    # OCIO_config/format knobs pre-seeded like a real nuke.root().
    state["root"]["fps"] = FakeKnob(24.0)
    state["root"]["first_frame"] = FakeKnob(1)
    state["root"]["last_frame"] = FakeKnob(100)
    state["root"]["colorManagement"] = FakeKnob("Nuke")
    state["root"]["OCIO_config"] = FakeKnob("nuke-default")
    initial_hd_format = FakeFormat(1920, 1080, 1.0, "HD")
    state["formats"].append(initial_hd_format)
    state["root"]["format"] = FakeFormatKnob(
        initial_hd_format, formats_lookup=lambda: state["formats"]
    )

    def root():
        return state["root"]

    def toNode(name):
        for n in state["nodes"]:
            if n.name() == name and not n._deleted:
                return n
        return None

    def createNode(node_class, inpanel=False):
        node = FakeNode(node_class=node_class)
        if node_class == "Read":
            node["file"] = FakeKnob("")
            node["first"] = FakeKnob(1)
            node["last"] = FakeKnob(1)
            node["origfirst"] = FakeKnob(1)
            node["origlast"] = FakeKnob(1)
            node._fake_width = 0
            node._fake_height = 0
        state["nodes"].append(node)
        return node

    def delete(node):
        node._deleted = True
        if node in state["nodes"]:
            state["nodes"].remove(node)

    def allNodes(node_class=None):
        if node_class is None:
            return [n for n in state["nodes"] if not n._deleted]
        return [n for n in state["nodes"] if not n._deleted and n.Class() == node_class]

    def addOnCreate(*a, **k):
        pass

    def addOnScriptLoad(*a, **k):
        pass

    def removeOnCreate(*a, **k):
        pass

    def removeOnScriptLoad(*a, **k):
        pass

    def Boolean_Knob(name):
        return FakeBooleanKnob(name)

    def formats():
        return list(state["formats"])

    def addFormat(tcl_string):
        # "width height pixel_aspect name"
        parts = tcl_string.split()
        width, height, pixel_aspect, name = int(parts[0]), int(parts[1]), float(parts[2]), parts[3]
        for f in state["formats"]:
            if f.name() == name:
                raise RuntimeError("format already exists with different size: %s" % name)
        fmt = FakeFormat(width, height, pixel_aspect, name)
        state["formats"].append(fmt)
        return fmt

    # Patch createNode("Read", ...) to look up its dimensions from
    # fake_disk once a file path is actually set on it - simplest way is
    # to wrap FakeKnob for "file" with a setValue that also updates
    # _fake_width/_fake_height on the owning node.
    orig_create_node = createNode

    def createNode_with_disk_lookup(node_class, inpanel=False):
        node = orig_create_node(node_class, inpanel=inpanel)
        if node_class == "Read":
            file_knob = node["file"]
            orig_set_value = file_knob.setValue

            def patched_set_value(v, _node=node):
                orig_set_value(v)
                w, h = fake_disk.get(v, (0, 0))
                _node._fake_width = w
                _node._fake_height = h

            file_knob.setValue = patched_set_value
        return node

    fake_nuke.root = root
    fake_nuke.toNode = toNode
    fake_nuke.createNode = createNode_with_disk_lookup
    fake_nuke.delete = delete
    fake_nuke.allNodes = allNodes
    fake_nuke.addOnCreate = addOnCreate
    fake_nuke.addOnScriptLoad = addOnScriptLoad
    fake_nuke.removeOnCreate = removeOnCreate
    fake_nuke.removeOnScriptLoad = removeOnScriptLoad
    fake_nuke.Boolean_Knob = Boolean_Knob
    fake_nuke.formats = formats
    fake_nuke.addFormat = addFormat
    fake_nuke._state = state  # exposed for test assertions
    return fake_nuke


# --------------------------------------------------------------------------
# Fake `sgtk` module
# --------------------------------------------------------------------------

class FakeTemplate:
    def __init__(self, definition):
        self._definition = definition

    def apply_fields(self, fields):
        return self._definition.format(**fields)


class FakeContext:
    def __init__(self, project=None, entity=None):
        self.project = project
        self.entity = entity

    def as_template_fields(self, template):
        # Only the fields this test suite's templates actually need.
        return {"Sequence": "EP_2", "Shot": self.entity["name"] if self.entity else None}


class FakeShotgun:
    def __init__(self, project_row=None, shot_row=None, publishes=None):
        self._project_row = project_row or {}
        self._shot_row = shot_row or {}
        self._publishes = publishes or []
        self.find_one_should_raise = False
        self.find_should_raise = False

    def find_one(self, entity_type, filters, fields):
        if self.find_one_should_raise:
            raise RuntimeError("simulated ShotGrid outage")
        if entity_type == "Project":
            return self._project_row
        if entity_type == "Shot":
            return self._shot_row
        return None

    def find(self, entity_type, filters, fields, order=None):
        if self.find_should_raise:
            raise RuntimeError("simulated ShotGrid outage")
        return self._publishes


class FakeApp:
    def __init__(self, shotgun, templates, roots=None):
        self.shotgun = shotgun
        self._templates = templates
        self.sgtk = types.SimpleNamespace(
            templates=self._templates,
            roots=roots or {"primary": "/jobs/SlateX"},
        )


def make_fake_sgtk_module(fake_app_holder):
    fake_sgtk = types.ModuleType("sgtk")

    class _Logger:
        def info(self, *a, **k): pass
        def debug(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass

    def get_logger(name):
        return _Logger()

    def current_bundle():
        return fake_app_holder["app"]

    fake_engine_holder = fake_app_holder  # reuse same dict for engine.context

    def current_engine():
        return types.SimpleNamespace(context=fake_app_holder["context"])

    fake_sgtk.platform = types.SimpleNamespace(
        get_logger=get_logger,
        current_bundle=current_bundle,
        current_engine=current_engine,
    )
    return fake_sgtk


# --------------------------------------------------------------------------
# Pytest fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def plate_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def handler_module(monkeypatch, plate_dir):
    """
    Builds fresh fake nuke/sgtk modules, injects them into sys.modules,
    (re)imports the handler module fresh, and returns it along with the
    fake modules/app for test setup. Each test gets an isolated handler
    module instance (importlib reload) so FakeNode's class-level registry
    doesn't leak state across tests.
    """
    import importlib

    fake_disk = {}  # populated per-test via a returned setter

    fake_nuke = make_fake_nuke_module(fake_disk)
    monkeypatch.setitem(sys.modules, "nuke", fake_nuke)

    fake_app_holder = {"app": None, "context": None}
    fake_sgtk = make_fake_sgtk_module(fake_app_holder)
    monkeypatch.setitem(sys.modules, "sgtk", fake_sgtk)

    # Ensure a clean FakeNode registry per test.
    FakeNode._all_nodes_registry = []

    handler_pkg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "python",
    )
    if handler_pkg_path not in sys.path:
        sys.path.insert(0, handler_pkg_path)

    import tk_nuke_projectsettings.handler as handler_mod
    importlib.reload(handler_mod)

    return {
        "handler_mod": handler_mod,
        "fake_nuke": fake_nuke,
        "fake_disk": fake_disk,
        "fake_app_holder": fake_app_holder,
        "plate_dir": plate_dir,
    }


def _write_fake_exr_sequence(plate_dir, shot, first, last, width=None, height=None):
    """Creates empty placeholder files named like a real ingested EXR
    sequence (frame-number detection only looks at filenames/existence,
    not real EXR bytes - the fake Read node supplies the "metadata")."""
    paths = []
    for frame in range(first, last + 1):
        p = os.path.join(plate_dir, "%s.%04d.exr" % (shot, frame))
        open(p, "w").close()
        paths.append(p)
    return paths


def _setup_handler(hm, project_row=None, shot_row=None, publishes=None,
                    shot_name="STRM_E2_0010"):
    handler_mod = hm["handler_mod"]
    fake_disk = hm["fake_disk"]

    shotgun = FakeShotgun(project_row=project_row, shot_row=shot_row, publishes=publishes)
    shot_plate_template = FakeTemplate("{shot_plate_root}/{Shot}/{Shot}.{SEQ}.exr".replace(
        "{shot_plate_root}", "Projects/str/Plates/{Sequence}"
    ))
    templates = {
        "shot_work_area_nuke": FakeTemplate("Artists/str/{Sequence}/{Shot}/{Step}/Nuke/{Step}"),
        "shot_plate": shot_plate_template,
    }
    app = FakeApp(shotgun=shotgun, templates=templates)
    hm["fake_app_holder"]["app"] = app
    context = FakeContext(
        project={"id": 91, "type": "Project"},
        entity={"id": 5827, "type": "Shot", "name": shot_name},
    )
    hm["fake_app_holder"]["context"] = context

    handler = handler_mod.NukeProjectSettingsHandler()
    return handler, context, fake_disk


# --------------------------------------------------------------------------
# Tests: FPS
# --------------------------------------------------------------------------

class TestFPS:
    def test_fps_applied_from_shotgrid(self, handler_module):
        handler, context, _ = _setup_handler(
            handler_module, project_row={"sg_fps": 25.0}
        )
        fps = handler._get_fps(context)
        assert fps == 25.0

    def test_fps_none_when_not_set(self, handler_module):
        handler, context, _ = _setup_handler(
            handler_module, project_row={"sg_fps": None}
        )
        assert handler._get_fps(context) is None

    def test_fps_falls_back_to_env_var_on_sg_error(self, handler_module, monkeypatch):
        handler, context, _ = _setup_handler(handler_module, project_row={"sg_fps": 30.0})
        handler.app.shotgun.find_one_should_raise = True
        monkeypatch.setenv("NFA_PROJECT_FPS", "23.976")
        fps = handler._get_fps(context)
        assert fps == pytest.approx(23.976)


# --------------------------------------------------------------------------
# Tests: frame range
# --------------------------------------------------------------------------

class TestFrameRange:
    def test_frame_range_from_shotgrid_cut_fields(self, handler_module):
        handler, context, _ = _setup_handler(
            handler_module, shot_row={"sg_cut_in": 1001, "sg_cut_out": 1086}
        )
        first, last = handler._get_frame_range(context)
        assert (first, last) == (1001, 1086)

    def test_frame_range_none_when_cut_fields_unset(self, handler_module):
        handler, context, _ = _setup_handler(
            handler_module, shot_row={"sg_cut_in": None, "sg_cut_out": None}
        )
        assert handler._get_frame_range(context) == (None, None)


# --------------------------------------------------------------------------
# Tests: color management (OCIO)
# --------------------------------------------------------------------------

class TestColorManagement:
    def test_ocio_applied(self, handler_module):
        handler, context, _ = _setup_handler(handler_module)
        root = handler_module["fake_nuke"].root()
        handler._apply_ocio(root)
        assert root["colorManagement"].value() == "OCIO"
        assert root["OCIO_config"].value() == "aces_1.2"


# --------------------------------------------------------------------------
# Tests: full-size format from EXR metadata (the new behavior)
# --------------------------------------------------------------------------

class TestFormatFromPlate:
    def test_format_set_from_exr_dimensions_1920x1080(self, handler_module):
        hm = handler_module
        plate_dir = hm["plate_dir"]
        shot = "STRM_E2_0010"
        paths = _write_fake_exr_sequence(plate_dir, shot, 1001, 1010)
        # Tell the fake Read node what "reading the EXR" should report.
        hm["fake_disk"][paths[0].replace(os.sep, "/")] = (1920, 1080)

        handler, context, _ = _setup_handler(hm, shot_name=shot)
        root = hm["fake_nuke"].root()

        printf_path = os.path.join(plate_dir, "%s.%%04d.exr" % shot).replace(os.sep, "/")
        width, height = handler._read_exr_dimensions(printf_path, 1001, 1010)
        assert (width, height) == (1920, 1080)

        handler._apply_format_from_plate(root, width, height)
        assert root["format"].value().name() == "sx_plate_1920x1080"
        assert root["format"].value().width() == 1920
        assert root["format"].value().height() == 1080

    def test_format_derived_not_hardcoded_for_different_resolution(self, handler_module):
        """A 4K plate must NOT produce the same format as a 1080p one --
        proves the format genuinely comes from the metadata, not a
        constant."""
        hm = handler_module
        plate_dir = hm["plate_dir"]
        shot = "STRM_E3_0040"
        paths = _write_fake_exr_sequence(plate_dir, shot, 1001, 1002)
        hm["fake_disk"][paths[0].replace(os.sep, "/")] = (3840, 2160)

        handler, context, _ = _setup_handler(hm, shot_name=shot)
        root = hm["fake_nuke"].root()

        printf_path = os.path.join(plate_dir, "%s.%%04d.exr" % shot).replace(os.sep, "/")
        width, height = handler._read_exr_dimensions(printf_path, 1001, 1002)
        assert (width, height) == (3840, 2160)
        handler._apply_format_from_plate(root, width, height)
        assert root["format"].value().name() == "sx_plate_3840x2160"
        assert root["format"].value().name() != "sx_plate_1920x1080"

    def test_missing_exr_file_returns_none_no_hardcoded_fallback(self, handler_module):
        """If the sample frame doesn't exist on disk, dimensions must
        come back as (None, None) -- apply_settings() must then leave
        format untouched rather than defaulting to e.g. 1920x1080."""
        hm = handler_module
        handler, context, _ = _setup_handler(hm)
        printf_path = "/nonexistent/plate/dir/SHOT.%04d.exr"
        width, height = handler._read_exr_dimensions(printf_path, 1001, 1010)
        assert (width, height) == (None, None)

    def test_no_plate_path_returns_none(self, handler_module):
        hm = handler_module
        handler, context, _ = _setup_handler(hm)
        assert handler._read_exr_dimensions(None, None, None) == (None, None)
        assert handler._read_exr_dimensions("", 1001, 1010) == (None, None)

    def test_zero_dimensions_treated_as_unknown(self, handler_module):
        """A fake/corrupt read reporting 0x0 must be treated the same as
        a missing file -- never propagated as a real format."""
        hm = handler_module
        plate_dir = hm["plate_dir"]
        shot = "STRM_E4_0001"
        paths = _write_fake_exr_sequence(plate_dir, shot, 1001, 1001)
        hm["fake_disk"][paths[0].replace(os.sep, "/")] = (0, 0)

        handler, context, _ = _setup_handler(hm, shot_name=shot)
        printf_path = os.path.join(plate_dir, "%s.%%04d.exr" % shot).replace(os.sep, "/")
        width, height = handler._read_exr_dimensions(printf_path, 1001, 1001)
        assert (width, height) == (None, None)

    def test_reusing_same_resolution_does_not_duplicate_format(self, handler_module):
        hm = handler_module
        plate_dir = hm["plate_dir"]
        handler, context, _ = _setup_handler(hm)
        root = hm["fake_nuke"].root()

        handler._apply_format_from_plate(root, 2048, 858)
        count_after_first = len(hm["fake_nuke"].formats())
        handler._apply_format_from_plate(root, 2048, 858)
        count_after_second = len(hm["fake_nuke"].formats())

        assert count_after_first == count_after_second  # no duplicate registered
        assert root["format"].value().name() == "sx_plate_2048x858"

    def test_throwaway_probe_node_is_cleaned_up(self, handler_module):
        """The throwaway Read node used to probe dimensions must not be
        left behind in the script."""
        hm = handler_module
        plate_dir = hm["plate_dir"]
        shot = "STRM_E2_0099"
        paths = _write_fake_exr_sequence(plate_dir, shot, 1001, 1001)
        hm["fake_disk"][paths[0].replace(os.sep, "/")] = (1280, 720)

        handler, context, _ = _setup_handler(hm, shot_name=shot)
        printf_path = os.path.join(plate_dir, "%s.%%04d.exr" % shot).replace(os.sep, "/")
        nodes_before = len(hm["fake_nuke"].allNodes())
        handler._read_exr_dimensions(printf_path, 1001, 1001)
        nodes_after = len(hm["fake_nuke"].allNodes())
        assert nodes_after == nodes_before


# --------------------------------------------------------------------------
# Tests: plate root directory resolution (now template-derived, not
# hand-joined) -- see handler.py's _find_plate_root_dir rewrite.
# --------------------------------------------------------------------------

class TestPlateRootDirResolution:
    def test_plate_root_derived_from_shot_plate_template(self, handler_module):
        hm = handler_module
        handler, context, _ = _setup_handler(hm, shot_name="STRM_E2_0010")
        plate_root = handler._find_plate_root_dir(context)
        # shot_plate template used in this test fixture resolves to
        # Projects/str/Plates/{Sequence}/{Shot}/{Shot}.{SEQ}.exr, so the
        # parent directory should be Projects/str/Plates/EP_2/STRM_E2_0010
        assert plate_root == "Projects/str/Plates/EP_2/STRM_E2_0010"

    def test_plate_root_none_when_template_missing(self, handler_module):
        hm = handler_module
        handler, context, _ = _setup_handler(hm)
        handler.app.sgtk.templates.pop("shot_plate")
        assert handler._find_plate_root_dir(context) is None
