import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from optics_photonics import composition, environments, jobs
from optics_photonics.mcp import tool_result
from support import workspace


class CompositionTests(unittest.TestCase):
    def test_catalogue_references_and_read_only_plan(self):
        view = composition.catalogue_view()
        ids = {module['id'] for module in view['modules']}
        self.assertEqual(ids, {item['id'] for item in view['components']})
        for field in view['fields']:
            for direction in field['directions']:
                self.assertLessEqual(set(direction['modules']), ids)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'OPTICS_HOME': str(Path(tmp) / 'home')}):
            result = composition.plan(['tmm', 'opticstudio'])
            self.assertEqual([item['action'] for item in result['actions']], ['install', 'connect'])
            self.assertFalse(environments.home().exists())

    def test_prepare_local_wheel_and_preserve_manual_connections(self):
        with workspace() as tmp, patch.dict(os.environ, {'OPTICS_HOME': str(Path(tmp) / 'home'), 'UV': '/unavailable/uv'}):
            wheels = Path(tmp) / 'wheels'
            wheels.mkdir()
            files = {'optics_fixture/__init__.py': 'def square(x): return x*x\n',
                     'optics_fixture-1.0.dist-info/METADATA': 'Metadata-Version: 2.1\nName: optics-fixture\nVersion: 1.0\n',
                     'optics_fixture-1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nGenerator: local\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
            files['optics_fixture-1.0.dist-info/RECORD'] = '\n'.join(name + ',,' for name in files) + '\n'
            with zipfile.ZipFile(wheels / 'optics_fixture-1.0-py3-none-any.whl', 'w') as wheel:
                for name, value in files.items():
                    wheel.writestr(name, value)
            environments.register('fixture-wheel', 'Local wheel', 'Offline calculation', modules=['optics_fixture'],
                                  pip=['optics-fixture==1.0'])
            with patch.dict(os.environ, {'PIP_NO_INDEX': '1', 'PIP_FIND_LINKS': str(wheels)}):
                response = tool_result({'name': 'environment_prepare', 'task': {}, 'arguments': {
                    'selections': ['fixture-wheel', 'opticstudio'], 'request_id': 'local-wheel'}})
                task = response['task']['taskId']
                result = composition.task_rpc('tasks/result', task)
                self.assertFalse(result['isError'], result)
                prepared = result['structuredContent']
                self.assertEqual(prepared['pending'][0]['component'], 'opticstudio')
                command = prepared['ready'][0]['command']
                actual = subprocess.check_output([*command, '-c', 'from optics_fixture import square; print(square(6))'], text=True)
                self.assertEqual(actual.strip(), '36')
                self.assertEqual(composition.prepare(['fixture-wheel', 'opticstudio'], request_id='local-wheel')['run_id'], task)
                self.assertIn('fixture-wheel', composition.installed()['ready_modules'])
                self.assertNotIn('opticstudio', composition.installed()['ready_modules'])
                removed = tool_result({'name': 'environment_remove', 'arguments': {'selections': ['fixture-wheel']}})
                self.assertEqual(removed['structuredContent']['removed'], ['fixture-wheel'])
                self.assertFalse(Path(command[0]).exists())
                retry = composition.prepare(['fixture-wheel'], request_id='install-again')['run_id']
                composition.task_rpc('tasks/result', retry)
                release = Path(tmp) / 'continue-calculation'
                research = jobs.submit([{'id': 'square', 'tool': 'fixture-wheel',
                                         'source': 'import time\nfrom pathlib import Path\n'
                                         f'while not Path({str(release)!r}).exists(): time.sleep(0.01)\n'
                                         'from optics_fixture import square\nRESULT={"square": square(7)}'}])['run_id']
                active = composition.remove(['fixture-wheel'])
                release.touch()
                self.assertEqual(active['removed'], [])
                self.assertIn('fixture-wheel', environments.read()['tools'])
                deadline = time.monotonic() + 15
                while (state := jobs.status(research))['state'] not in jobs.TERMINAL and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(state['state'], 'succeeded', state)
                preserved = composition.remove(['fixture-wheel'])
                self.assertEqual(preserved['removed'], ['fixture-wheel'])
                self.assertEqual(preserved['files'], [])
                self.assertTrue(Path(command[0]).exists())
                self.assertTrue((environments.home() / 'runs' / research).is_dir())
                self.assertEqual(jobs.status(research)['state'], 'succeeded')

    def test_uninstall_shared_and_external_connections(self):
        with workspace() as tmp, patch.dict(os.environ, {'OPTICS_HOME': tmp}):
            prefix = environments.home() / 'environments/shared'
            prefix.mkdir(parents=True)
            executable = prefix / 'solver'
            executable.write_text('owned solver')
            environments.save({'tools': {
                'tmm': {'kind': 'command', 'command': [str(executable)], 'managed': 'shared'},
                'grcwa': {'kind': 'command', 'command': [str(executable)], 'managed': 'shared'},
                'external': {'kind': 'python', 'python': sys.executable}}})
            result = composition.remove(['tmm', 'external'])
            self.assertTrue(executable.exists())
            self.assertTrue(Path(sys.executable).exists())
            self.assertIn(str(prefix), result['retained'])
            result = composition.remove(['grcwa'])
            self.assertFalse(prefix.exists())
            self.assertEqual(result['files'], [str(prefix)])
            prefix.mkdir()
            executable.write_text('owned solver')
            environments.save({'tools': {name: {'kind': 'command', 'command': [str(executable)], 'managed': 'shared'}
                                         for name in ('tmm', 'grcwa')}})
            result = composition.remove(['tmm', 'grcwa'])
            self.assertEqual(result['removed'], ['grcwa', 'tmm'])
            self.assertEqual(result['files'], [str(prefix)])

    def test_connection_only_and_empty_selections_keep_results(self):
        with workspace() as tmp, patch.dict(os.environ, {'OPTICS_HOME': tmp}):
            for names in ([], ['opticstudio']):
                response = tool_result({'name': 'environment_prepare', 'task': {}, 'arguments': {'selections': names}})
                task = response['task']['taskId']
                result = composition.task_rpc('tasks/result', task)
                self.assertEqual(result['structuredContent']['modules'], names)
                self.assertEqual(result['structuredContent']['ready'], [])
                self.assertEqual(composition.task(task)['status'], 'completed')
                with self.assertRaises(ValueError):
                    composition.task_rpc('tasks/cancel', task)


if __name__ == '__main__':
    unittest.main()
