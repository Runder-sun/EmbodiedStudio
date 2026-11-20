"""Check the real thumbnail function's SDK lifecycle without starting Isaac/RTX.

CameraSensor's installed SDK documentation and tests require play(commit=True)
before fetching data. Only that boundary is simulated; pixels below are test
fixtures and are never published as asset evidence.
"""
import argparse
import ast
from contextlib import nullcontext
import itertools
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np


KERNEL = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = KERNEL / 'runtime' / 'simulation.py'
sys.path.insert(0, str(KERNEL))
from runtime.jobs import measure


class ThumbnailLifecycleTests(unittest.TestCase):
    def test_thumbnail_starts_timeline_before_fetching_rgb(self):
        # Execute this function's actual AST without importing the SDK or the
        # rest of simulation.py (Mac has no Isaac installation).
        source = RUNTIME_SOURCE.read_text()
        tree = ast.parse(source, filename=str(RUNTIME_SOURCE))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'render_asset_thumbnail')
        calls, state = [], {'playing': False}
        image = np.broadcast_to(np.arange(1280, dtype=np.uint8)[None, :, None],
                                (720, 1280, 3)).copy()
        pixels = SimpleNamespace(numpy=lambda: image)

        def play(commit=False):
            self.assertTrue(commit)
            calls.append('play')
            state['playing'] = True

        sensor = MagicMock()
        sensor.get_data.side_effect = lambda *args, **kwargs: (
            pixels if state['playing'] else None, {})
        clock = itertools.count(0, 50)
        namespace = {
            'Path': Path, 'np': np, 'WIDTH': 1280, 'HEIGHT': 720,
            'time': SimpleNamespace(monotonic=lambda: next(clock)),
            '_ensure_app': lambda: None,
            'measure': measure,
            '_asset_scene': lambda node: SimpleNamespace(bounds=np.array([[-.5]*3, [.5]*3])),
            'normalize_assets': lambda world: world,
            '_asset_parts': lambda node: [],
            '_write_camera': lambda *args: None,
            '_setup_stage': lambda path: None,
            '_close_stage': lambda: calls.append('close'),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]),
                     str(RUNTIME_SOURCE), 'exec'), namespace)
        modules = {}

        def module(name, **fields):
            value = ModuleType(name)
            value.__dict__.update(fields)
            modules[name] = value

        for name in ('isaacsim', 'isaacsim.core', 'isaacsim.core.experimental',
                     'isaacsim.core.experimental.utils', 'isaacsim.sensors',
                     'isaacsim.sensors.experimental'):
            module(name)
        module('isaacsim.core.experimental.utils.app', play=play)
        module('isaacsim.core.experimental.utils.backend', use_backend=lambda *a, **k: nullcontext())
        module('isaacsim.core.rendering_manager',
               RenderingManager=SimpleNamespace(render=lambda: calls.append('render')))
        module('isaacsim.sensors.experimental.rtx', RtxCamera=MagicMock(),
               CameraSensor=MagicMock(return_value=sensor))
        module('warp', empty=lambda *a, **k: None, uint8=object())
        module('pxr', Gf=MagicMock(), Usd=MagicMock(), UsdGeom=MagicMock(), UsdLux=MagicMock())

        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, modules):
            result = namespace['render_asset_thumbnail'](
                {'path': 'test-fixture.glb', 'up_axis': 'Z'}, temporary)
            self.assertTrue(Path(result).is_file())
        self.assertLess(calls.index('play'), calls.index('render'))
        self.assertEqual(calls[-1], 'close')
        sensor.detach_annotators.assert_called_once_with(sensor.annotators)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-source', type=Path, default=RUNTIME_SOURCE)
    args, remaining = parser.parse_known_args()
    RUNTIME_SOURCE = args.runtime_source
    unittest.main(argv=[sys.argv[0], *remaining])
