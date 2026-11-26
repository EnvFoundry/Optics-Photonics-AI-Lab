"""Use installed scientific environments, or prepare a separate Python environment."""
import errno
import hashlib
import json
import platform
import re
from contextlib import contextmanager, ExitStack
import time
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv
from .catalogue import software as catalogue_software, catalogue
from . import integrations


def home(path=None):
    return Path(path or os.environ.get('OPTICS_HOME') or Path.home() / '.optics-photonics').expanduser().resolve()


@contextmanager
def editing(path=None):
    directory = home(path)
    directory.mkdir(parents=True, exist_ok=True)
    with locked(directory/'connections.lock'):
        yield


@contextmanager
def locked(file, blocking=True):
    """Hold an exclusive lock on a file, across processes and on every system."""
    with Path(file).open('a+b') as stream:
        # Both systems can lock an empty file; do not write before owning it.
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if not blocking or error.errno != errno.EACCES:
                        raise
                    time.sleep(.02)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def read(path=None):
    if not (home(path) / 'connections.json').exists():
        return {'tools': {}}
    # Windows file replacement cannot overlap a reader's open file handle.
    with editing(path):
        return _read(path)


def _read(path=None):
    file = home(path) / 'connections.json'
    return json.loads(file.read_text(encoding='utf-8')) if file.exists() else {'tools': {}}


def save(value, path=None):
    with editing(path):
        _save(value, path)


def update(section, key, value, path=None):
    """Change one entry while retaining concurrent registrations."""
    with editing(path):
        configuration = _read(path)
        configuration.setdefault(section, {})[key] = value
        _save(configuration, path)


def _save(value, path=None):
    directory = home(path)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        temporary = stream.name
    try:
        os.replace(temporary, directory / 'connections.json')
    finally:
        Path(temporary).unlink(missing_ok=True)


def binding_paths(binding):
    values = [binding.get(key) for key in ('python', 'executable', 'project')]
    values += binding.get('paths') or []
    values += (binding.get('command') or [])[:1]
    return {Path(value).expanduser().absolute() for value in values if isinstance(value, str) and value}


def uninstall(tools, path=None):
    from . import jobs
    if not isinstance(tools, list) or any(not isinstance(name, str) for name in tools):
        raise ValueError('selections must contain software IDs')
    if not tools or not (home(path) / 'connections.json').exists():
        return {'removed': [], 'files': [], 'retained': []}
    removed, files, retained, reasons = [], [], set(), []
    folder = home(path) / 'environments'
    with jobs.database(path) as db, ExitStack() as locks:
        db.execute('BEGIN IMMEDIATE')
        initial = read(path)['tools']
        names = set(tools) & initial.keys()
        prefixes = {}
        busy = set()
        for name in names:
            managed = initial[name].get('managed')
            if isinstance(managed, str) and managed not in {'.', '..'} and Path(managed).name == managed:
                prefix = folder / managed
                if prefix.is_dir() and not prefix.is_symlink() and prefix.resolve().parent == folder.resolve():
                    prefixes[name] = prefix
        for prefix in sorted(set(prefixes.values())):
            try:
                locks.enter_context(locked(folder / (prefix.name + '.lock'), blocking=False))
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                busy.add(prefix)
        rows = list(db.execute('SELECT specification,environment,state FROM steps'))
        active = {json.loads(row['specification'])['tool'] for row in rows if row['state'] in {'queued', 'running'}}
        frozen = set().union(*(binding_paths(json.loads(row['environment'])) for row in rows)) if rows else set()
        with editing(path):
            config = _read(path)
            removable = {name for name in names if config['tools'].get(name) == initial[name]
                         and name not in active and prefixes.get(name) not in busy}
            for name in sorted(names):
                binding = config['tools'].get(name)
                prefix = prefixes.get(name)
                if binding != initial[name] or name in active or prefix in busy:
                    reason = 'Connection changed' if binding != initial[name] else 'Used by active work'
                    retained.update(map(str, binding_paths(binding or initial[name])))
                    reasons.append({'component': name, 'reason': reason})
                    continue
                other = [value for key, value in config['tools'].items() if key not in removable]
                shared = any(prefix and (value.get('managed') == prefix.name or
                             any(item.is_relative_to(prefix) for item in binding_paths(value))) for value in other)
                referenced = prefix and any(item.is_relative_to(prefix) for item in frozen)
                if prefix and not shared and not referenced:
                    if prefix.exists():
                        shutil.rmtree(prefix)
                    files.append(str(prefix))
                else:
                    retained.update([str(prefix)] if prefix else map(str, binding_paths(binding)))
                    reasons.append({'component': name, 'reason': 'Shared installation' if shared else
                                    'Retained research environment' if referenced else 'External installation'})
                del config['tools'][name]
                removed.append(name)
            _save(config, path)
    return {'removed': removed, 'files': sorted(set(files)), 'retained': sorted(retained), 'reasons': reasons}


def register(tool, name, description, *, kind='python', modules=None, distributions=None, sources=None,
             pip=None, conda=None, python=None, platforms=None, path=None):
    """Describe a local research tool without changing the published software catalogue.

    pip, conda, python and platforms form an installation recipe as in the
    catalogue, so that prepare_environment and submissions install it.
    """
    if not isinstance(tool, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,99}', tool):
        raise ValueError('Use a lowercase software ID with letters, digits, dots, hyphens or underscores')
    if any(item['id'] == tool for item in catalogue()):
        raise ValueError('This ID belongs to the published catalogue or an installed integration; bind its environment directly')
    if not isinstance(name, str) or not name.strip() or not isinstance(description, str) or not description.strip():
        raise ValueError('Provide a software name and description')
    if kind not in ('python', 'command'):
        raise ValueError('kind must be python or command')
    values = {'modules': [] if modules is None else modules, 'distributions': [] if distributions is None else distributions,
              'sources': [] if sources is None else sources, 'pip': [] if pip is None else pip, 'conda': [] if conda is None else conda}
    for key, items in values.items():
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(key + ' must be an array of nonempty strings')
    if (pip or conda) and kind != 'python':
        raise ValueError('An installation recipe applies to Python software')
    recipe_fields = {}
    if python is not None:
        satisfies('3', python)  # rejects a malformed specifier
        recipe_fields['python'] = python
    if platforms is not None:
        if not isinstance(platforms, list) or any(item not in PLATFORMS for item in platforms):
            raise ValueError('platforms takes names from: ' + ', '.join(PLATFORMS))
        recipe_fields['platforms'] = platforms
    definition = {'id': tool, 'name': name, 'summary': description, 'origin': 'local',
                  'connection': {'kind': kind, **values, **recipe_fields}}
    update('software', tool, definition, path)
    return definition


def software(tool, language='en', path=None):
    configured = read(path)
    local = configured.get('software', {}).get(tool)
    if local is not None:
        return local
    try:
        return catalogue_software(tool, language)
    except ValueError:
        # Removing an integration does not invalidate an already connected
        # interpreter or the definition retained with its scientific runs.
        bound = configured['tools'].get(tool, {}).get('definition')
        if bound and bound.get('origin') == 'integration':
            return bound
        raise


REPEATED = ('interface', 'sources', 'upstream', 'native_program', 'routes')
# A brief reply shows these fields of a catalogue entry and leaves out of its connection record what the entry
# already states and the sentences that are the same for every software.
ENTRY = ('id', 'name', 'group', 'cats', 'method', 'summary', 'api', 'license', 'compute', 'usage', 'limit', 'sources')
UNIFORM = ('execution', 'environment', 'session_adapter')


@integrations.synchronized
def details(tool, language='en', path=None, program=None, source=True, brief=False):
    """One software entry with its connection, its routes and what it can run.

    With program, that packaged program's physics, inputs, results and checks. source=False omits its text;
    Python callers retain the earlier source-including default, while MCP requests source only when needed.
    brief=True, which MCP also requests, states each fact once: the programs by name with the names of their
    inputs, an upstream MCP server or Skill only where one was located, and no sentence common to all software.
    """
    from . import integrations, operations, programs, routes
    item = software(tool, language, path)
    if program is not None:
        spec = programs.details(tool, program)
        how = 'script' if spec.get('language') else 'source'
        reply = {'tool': tool, 'program': program, **{k: v for k, v in spec.items() if k != 'name'}}
        if source:
            return {**reply, 'source': programs.source(tool, program),
                    'adapt': f'Submit an edited copy as {how}.' + (' Supply the declared inputs; scripts do not receive packaged defaults.' if spec.get('language') else
                                                                  ' parameters(PROGRAM, INPUTS) checks the inputs it declares.')}
        return {**reply, 'run': f'submit_calculation(tool, action="{program}", inputs={{...}}) replaces defaults; '
                                f'source=true here returns the program text, to adapt and submit as {how}.'}
    if item.get('origin') != 'local':
        # connections.json repeats these catalogue fields so that the file can be read on its own.
        item = {**item, 'connection': {key: value for key, value in item['connection'].items() if key not in REPEATED}}
    found = routes.routes(tool, path)
    references = integrations.references(tool, language)
    guides = {'integration_guides': references} if references else {}
    if not brief:
        return {**item, **guides, 'routes': found, 'execution': {'programs': programs.details(tool), **operations.details(tool)}}
    if item.get('origin') != 'local':
        located = {kind: item[kind] for kind in ('mcp', 'skill') if item[kind]['kind'] != 'unverified'}
        item = {**{key: item[key] for key in ENTRY}, **({'upstream': located} if located else {}),
                **({'integration': item['integration']} if item.get('integration') else {}),
                'connection': {key: value for key, value in item['connection'].items() if key not in UNIFORM}}
    listed = programs.listing(tool)
    hint = {'program_details': 'software_details(tool, program=NAME) gives the physics, input defaults and meanings, '
                               'results and checks'} if listed else {}
    return {**item, **guides, 'routes': found, 'execution': {'programs': listed, **hint, **operations.details(tool, brief=True)}}


def registered(path=None):
    return list(read(path).get('software', {}).values())


INSPECT = """import json, sys
if sys.version_info < (3, 9):
 sys.exit('This interpreter is Python %s; calculations need Python 3.9 or newer in the software environment' % sys.version.split()[0])
import importlib.util, importlib.metadata
modules=json.loads(sys.argv[1]); distributions=json.loads(sys.argv[2])
found={}
for name in modules:
 try: found[name]=importlib.util.find_spec(name) is not None
 except Exception: found[name]=False
versions={}
for name in distributions:
 try: versions[name]=importlib.metadata.version(name)
 except importlib.metadata.PackageNotFoundError: versions[name]=None
print('\\nOPTICS-INSPECT '+json.dumps({'python':sys.executable,'python_version':sys.version.split()[0], 'prefix':sys.prefix,'base_prefix':sys.base_prefix,'modules':found,'versions':versions}))
"""


def search_path(paths, env=None):
    """Process variables that put module folders shipped with an installation first on the Python path."""
    env = dict(os.environ if env is None else env)
    if paths:
        env['PYTHONPATH'] = os.pathsep.join([*paths, *filter(None, [env.get('PYTHONPATH')])])
    return env


def inspect(python, modules, distributions, timeout=60, paths=None):
    """Which modules an interpreter finds and which distribution versions it has, in one subprocess."""
    # Resolving a venv's symlink would select the base interpreter and lose its packages.
    interpreter = os.path.abspath(os.path.expanduser(str(python or sys.executable)))
    # No child reads the server's stdin: on Windows a child that inherits the MCP pipe waits
    # at start-up for the server's pending read of it, which ends only with the next request.
    result = subprocess.run([interpreter, '-c', INSPECT, json.dumps(modules), json.dumps(distributions)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=search_path(paths))
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-2000:] or 'Could not inspect this interpreter')
    # Packages may print while they load or exit (Meep reports its run time); the reply carries a marker.
    line = next(line for line in reversed(result.stdout.splitlines()) if line.startswith('OPTICS-INSPECT '))
    return json.loads(line[len('OPTICS-INSPECT '):])


def modules(tool, path=None):
    """Modules that show a tool is importable: its recipe's, or those of its Python or service route."""
    record = software(tool, path=path)['connection']
    return record.get('modules') or next((route['modules'] for route in record.get('routes', [])
                                          if route['route'] in ('python', 'service') and route.get('modules')), [])


def probe(tool, python=None, path=None, paths=None):
    record = software(tool, path=path)['connection']
    status = inspect(python, modules(tool, path), record.get('distributions', []), paths=paths)
    status.update(tool=tool, available=all(status['modules'].values()))
    return status


def settings(threads=1, concurrency=1, cwd=None, requirements=None, env=None):
    """Validated execution settings of a binding."""
    if type(threads) is not int or threads < 1 or type(concurrency) is not int or concurrency < 1:
        raise ValueError('threads and concurrency must be positive integers')
    if cwd is not None:
        cwd = str(Path(cwd).expanduser().resolve())
        if not Path(cwd).is_dir():
            raise ValueError('Working directory does not exist')
    from .jobs import quantities
    quantities(requirements or {})
    if env is not None and (not isinstance(env, dict) or any(not isinstance(k, str) or
        not isinstance(v, str) for k, v in env.items())):
        raise ValueError('env must map environment variable names to strings')
    return {'threads': threads, 'concurrency': concurrency, 'cwd': cwd, 'requirements': requirements or {}, 'env': env or {}}


def bind(tool=None, *, python=None, command=None, path=None, threads=1, concurrency=1, cwd=None,
         requirements=None, env=None, paths=None):
    """Connect a tool to an interpreter or executable; without a tool, every tool the interpreter provides.

    paths lists folders, such as a module folder inside an installation, put first on the Python path.
    """
    chosen = settings(threads, concurrency, cwd, requirements, env)
    if tool is None:
        if command or not python or paths:
            raise ValueError('Name the tool, or give python alone to bind every tool that interpreter provides')
        return bind_every(python, chosen, path)
    if paths is not None:
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p for p in paths):
            raise ValueError('paths is an array of folders')
        paths = [str(Path(p).expanduser().resolve()) for p in paths]
        if any(not Path(p).is_dir() for p in paths):
            raise ValueError('A folder in paths does not exist')
    item = software(tool, path=path)
    if command:
        if not isinstance(command, list) or not all(isinstance(v, str) and v for v in command):
            raise ValueError('command must be an executable and argument array')
        executable = shutil.which(command[0]) or (str(Path(command[0]).resolve()) if Path(command[0]).is_file() else None)
        if not executable:
            raise ValueError('Executable was not found: ' + command[0])
        connection = {'kind': 'command', 'command': [executable, *command[1:]]}
    else:
        if item['connection']['kind'] != 'python' and not modules(tool, path):
            raise ValueError('This software has no Python module; bind_native_software connects its executable '
                             'or automation server, and bind_environment(command=...) an executable of your own')
        inspected = probe(tool, python, path, paths)
        if not inspected['available']:
            missing = [k for k, v in inspected['modules'].items() if not v]
            raise ValueError('Missing modules in the selected interpreter: ' + ', '.join(missing) +
                             ('. prepare_environment installs this tool' if recipe(tool, path) else
                              '. A module that ships with the installation needs its folder in paths (sdk_path)'))
        connection = {'kind': 'python', 'python': inspected['python'], 'versions': inspected['versions'], **({'paths': paths} if paths else {})}
    connection.update(chosen)
    if item.get('origin') in ('local', 'integration'):
        connection['definition'] = item
    before = read(path)['tools'].get(tool) or {}
    if before.get('managed') and before.get('python') == connection.get('python'):
        # New settings for a prepared environment keep what was installed into it.
        connection.update({key: before[key] for key in ('managed', 'installer', 'packages') if key in before})
    update('tools', tool, connection, path)
    return {'tool': tool, **connection}


def bind_every(python, chosen, path=None):
    """Bind each catalogue or registered Python tool whose modules one interpreter finds.

    Tools already connected to an environment keep it; bind them by name to change it.
    """
    from .catalogue import connections
    local = read(path).get('software', {})
    records = {**{tool: record for tool, record in connections().items()},
               **{tool: item['connection'] for tool, item in local.items()}}
    records = {tool: record for tool, record in records.items() if record['kind'] == 'python' and record.get('modules')}
    inspected = inspect(python, sorted({m for r in records.values() for m in r['modules']}),
                        sorted({d for r in records.values() for d in r.get('distributions', [])}), timeout=300)
    with editing(path):
        configuration = _read(path)
        tools = configuration.setdefault('tools', {})
        bound, kept = {}, []
        for tool, record in sorted(records.items()):
            if not all(inspected['modules'][m] for m in record['modules']):
                continue
            if tool in tools:
                kept.append(tool)
                continue
            versions = {d: inspected['versions'][d] for d in record.get('distributions', [])}
            tools[tool] = {'kind': 'python', 'python': inspected['python'], 'versions': versions, **chosen,
                           **({'definition': local[tool]} if tool in local else {})}
            bound[tool] = versions
        _save(configuration, path)
    return {'python': inspected['python'], 'python_version': inspected['python_version'],
            'bound': bound, 'kept': kept}


PLATFORMS = ('linux-64', 'linux-aarch64', 'osx-64', 'osx-arm64', 'win-64', 'win-arm64')
MICROMAMBA = 'https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-'


def here():
    """This computer as a conda platform name; recipes list the platforms they install on."""
    arm = platform.machine().lower() in ('arm64', 'aarch64')
    if sys.platform.startswith('linux'):
        return 'linux-aarch64' if arm else 'linux-64'
    if sys.platform == 'darwin':
        return 'osx-arm64' if arm else 'osx-64'
    return 'win-arm64' if arm else 'win-64'


def satisfies(version, specifier):
    """Whether a version such as 3.12.4 meets a specifier such as >=3.10,<3.13.

    Each clause compares as many components as it names, so ==3.11 accepts 3.11.9.
    """
    have = [int(part) for part in re.findall(r'\d+', version)]
    for clause in filter(None, (part.strip() for part in str(specifier or '').split(','))):
        found = re.fullmatch(r'(>=|<=|==|!=|>|<)?\s*(\d+(?:\.\d+)*)', clause)
        if not found:
            raise ValueError('Use a Python version specifier such as >=3.10,<3.13, not ' + clause)
        want = [int(part) for part in found[2].split('.')]
        value = (have + [0] * len(want))[:len(want)]
        if not {'>=': value >= want, '<=': value <= want, '>': value > want, '<': value < want,
                '!=': value != want}.get(found[1] or '==', value == want):
            return False
    return True


def recipe(tool, path=None):
    """What preparing a tool installs, or None when the software has its own installer.

    pip holds requirements for pip or uv; conda holds conda-forge packages for
    software without wheels. python is a version specifier, platforms lists the
    conda platform names it installs on, and environment_of names the tool whose
    environment it shares (MPB comes with Meep).
    """
    record = software(tool, path=path)['connection']
    if not record.get('pip') and not record.get('conda') and not record.get('julia'):
        return None
    return {'pip': list(record.get('pip', [])), 'conda': list(record.get('conda', [])), 'python': record.get('python'),
            'platforms': record.get('platforms'), 'environment': record.get('environment_of') or tool,
            'julia': list(record.get('julia', [])), 'executable': record.get('executable')}


def installable(tool, path=None):
    """The recipe for this computer; a ValueError says what to do when there is none."""
    item = recipe(tool, path)
    if item is None:
        note = software(tool, path=path)['connection'].get('installation_note')
        from . import routes
        kinds = {route['route'] for route in routes.routes(tool, path)}
        then = ('bind_environment with its interpreter' if 'python' in kinds and not kinds & {'executable', 'automation', 'session'} else
                'discover_native_software and bind_native_software' if kinds & {'executable', 'automation', 'session', 'python', 'service'} else
                'run its own scripts in it; programs here prepare their input files and read the files it exports')
        raise ValueError(tool + ' uses its own installer' + (' (' + note.rstrip('.') + ')' if note else '') + '. Install it, then ' + then)
    if item['platforms'] and here() not in item['platforms']:
        note = software(tool, path=path)['connection'].get('installation_note')
        raise ValueError(f"{tool} installs on {', '.join(item['platforms'])}, not on {here()}. " + (note + ' ' if note else '') +
                         'Or use a computer with one of those platforms, for example through WSL or SSH')
    if item['julia'] and not julia(path):
        installable('julia', path)   # says how to install Julia here when conda-forge has none for this platform
    return item


def julia(path=None):
    """A Julia executable: the one bound to the julia tool, on PATH, or installed by juliaup."""
    bound = read(path)['tools'].get('julia')
    if bound and bound.get('kind') == 'executable' and Path(bound['executable']).is_file():
        return bound['executable']
    found = shutil.which('julia') or shutil.which('julia', path=str(Path.home() / '.juliaup' / 'bin'))
    return found


def interpreter(prefix, conda=False):
    if os.name == 'nt':
        return prefix / ('python.exe' if conda else 'Scripts/python.exe')
    return prefix / 'bin' / 'python'


def uv():
    found = os.environ.get('UV') or shutil.which('uv')
    return found if found and Path(found).is_file() else None


def conda(path=None):
    """A conda-compatible installer: OPTICS_CONDA, micromamba, mamba or conda, else micromamba fetched once.

    The fetched micromamba is the official mamba-org release for this platform,
    checked against its published SHA-256 and kept in OPTICS_HOME/bin.
    """
    for candidate in (os.environ.get('OPTICS_CONDA'), shutil.which('micromamba'), shutil.which('mamba'), shutil.which('conda')):
        if candidate and Path(candidate).is_file():
            return candidate
    target = home(path) / 'bin' / ('micromamba.exe' if os.name == 'nt' else 'micromamba')
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(MICROMAMBA + here(), timeout=300) as response:
            data = response.read()
        with urllib.request.urlopen(MICROMAMBA + here() + '.sha256', timeout=60) as response:
            expected = response.read().decode().split()[0].lower()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('The downloaded micromamba does not match its published SHA-256; nothing was installed')
        partial = target.with_name(target.name + '.partial')
        partial.write_bytes(data)
        partial.chmod(0o755)
        os.replace(partial, target)
    return str(target)


def install(command, conda_root=None):
    """Run an installer; its output goes to stderr so that stdout stays a JSON reply.

    conda installers share one package cache, which two processes extracting into it at once can
    leave failing on Windows, so they run one at a time.
    """
    if conda_root is not None:
        Path(conda_root).parent.mkdir(parents=True, exist_ok=True)
        with locked(Path(conda_root).parent / 'conda-installer.lock'):
            return _install(command, conda_root)
    return _install(command, conda_root)


def _install(command, conda_root=None):
    env = dict(os.environ)
    if conda_root and 'MAMBA_ROOT_PREFIX' not in env:
        env['MAMBA_ROOT_PREFIX'] = str(conda_root)
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=subprocess.PIPE, text=True, env=env)
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"{Path(command[0]).name} {' '.join(command[1:3])} failed with code {result.returncode}: "
                           + result.stderr.strip()[-1500:])
    sys.stderr.write(result.stderr)


def create(prefix, item, python, path):
    """A new environment for a recipe; returns the installer that will add packages to it."""
    if item['conda']:
        installer = conda(path)
        # Without a stated range, a Python with broad conda-forge coverage rather than the newest release.
        # A conda match spec reads ==3.9 as exactly 3.9; =3.9 accepts every 3.9 release.
        version = re.sub(r'^==', '=', item['python'] or '=3.12')
        interpreter = [] if item.get('executable') else ['python' + version]
        install([installer, 'create', '-y', '-q', '-p', str(prefix), '-c', 'conda-forge', '--override-channels',
                 *interpreter, *item['conda']], home(path) / 'conda')
        return 'conda'
    current = platform.python_version()
    base = str(Path(python).expanduser()) if python else None
    found = uv()
    if found:
        # The connector's own Python when it fits; otherwise one release with broad wheel coverage.
        version = base or (sys.executable if satisfies(current, item['python']) else
                           next((v for v in ('3.12', '3.13', '3.11', '3.14', '3.10') if satisfies(v, item['python'])), item['python']))
        install([found, 'venv', '--seed', '--quiet', '--python', version, str(prefix)])
        return 'uv'
    if base:
        install([base, '-m', 'venv', str(prefix)])
    elif satisfies(current, item['python']):
        venv.EnvBuilder(with_pip=True).create(prefix)
    else:
        raise ValueError(f"This recipe needs Python {item['python']}; the connector runs {current}. "
                         'Install uv, which provides that Python, or pass python with a matching interpreter')
    return 'pip'


def pip_install(executable, requirements):
    found = uv()
    if found:
        install([found, 'pip', 'install', '--quiet', '--python', str(executable), *requirements])
    else:
        install([str(executable), '-m', 'pip', 'install', '--quiet', *requirements])


def prepare(tool, *, path=None, python=None, packages=None):
    """Install a tool's recipe, and any further packages, into its environment under OPTICS_HOME; bind the tool.

    The environment is created once. Preparing again finds the tool importable
    and only adds packages; another process preparing the same environment
    waits for this one. packages also extends a managed environment of a tool
    without a recipe.
    """
    packages = [] if packages is None else packages
    if not isinstance(packages, list) or any(not isinstance(item, str) or not item.strip() for item in packages):
        raise ValueError('packages must be an array of requirement strings, such as scipy or h5py>=3')
    configured = read(path)['tools'].get(tool)
    if recipe(tool, path) is None and packages and configured and configured.get('managed'):
        item = {'pip': [], 'conda': [], 'python': None, 'platforms': None, 'environment': configured['managed'], 'julia': [], 'executable': None}
    else:
        item = installable(tool, path)
    if item['julia'] or item['executable']:
        return prepare_executable(tool, item, configured, packages, path=path)
    name = item['environment']
    folder = home(path) / 'environments'
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / name
    with locked(folder / (name + '.lock')):
        executable = interpreter(prefix, bool(item['conda']))
        created = not executable.exists()
        if created:
            if prefix.exists():
                shutil.rmtree(prefix)  # left by an installation that stopped before its interpreter existed
            installer = create(prefix, item, python, path)
        else:
            installer = (configured or {}).get('installer') or ('conda' if item['conda'] else 'uv' if uv() else 'pip')
        missing = created or not probe(tool, executable, path)['available']
        if missing and item['conda'] and not created:
            install([conda(path), 'install', '-y', '-q', '-p', str(prefix), '-c', 'conda-forge', '--override-channels',
                     *item['conda']], home(path) / 'conda')
        requirements = (item['pip'] if missing else []) + packages
        if requirements:
            pip_install(executable, requirements)
        # Settings chosen earlier for this tool (threads, resources, variables) stay. Software installed from a
        # recipe is open, so without earlier settings only the shared CPU capacity limits its processes.
        from .jobs import cores
        settings = {key: configured[key] for key in ('threads', 'concurrency', 'cwd', 'requirements', 'env')
                    if configured and configured.get(key) is not None} if configured else {'concurrency': cores()}
        connection = bind(tool, python=executable, path=path, **settings)
        if item['conda']:
            # conda-forge packages may not carry the metadata that pip reads; ask the installer.
            listed = subprocess.run([conda(path), 'list', '-p', str(prefix), '--json'], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    env={**os.environ, 'MAMBA_ROOT_PREFIX': os.environ.get('MAMBA_ROOT_PREFIX', str(home(path) / 'conda'))})
            names = {re.split(r'[<>=! ]', spec)[0] for spec in item['conda']}
            if not listed.returncode:
                entries = json.loads(listed.stdout)
                entries = entries.get('packages', []) if isinstance(entries, dict) else entries  # micromamba 2 or conda
                connection['versions'] = {**connection['versions'], **{entry['name']: entry['version'] for entry in entries
                                                                         if entry['name'] in names}}
        managed = {**{k: v for k, v in connection.items() if k != 'tool'}, 'managed': name, 'installer': installer,
                   'packages': sorted(set((configured or {}).get('packages', [])) | set(packages))}
        update('tools', tool, managed, path)
    return {'tool': tool, **managed}


def prepare_executable(tool, item, configured, packages=(), path=None):
    """An executable from conda-forge, or a Julia project environment with the recipe's packages; bound by its route.

    packages adds Julia packages to a Julia project, or conda-forge packages to an executable's environment.
    """
    from . import routes
    from .jobs import cores
    folder = home(path) / 'environments'
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / item['environment']
    kept = {key: configured[key] for key in ('threads', 'concurrency', 'cwd', 'requirements', 'env')
            if configured and configured.get(key) is not None} if configured else {'concurrency': cores()}
    with locked(folder / (item['environment'] + '.lock')):
        if item['julia']:
            executable = julia(path)
            if not executable:
                executable = prepare('julia', path=path)['executable']
            prefix.mkdir(exist_ok=True)
            names = ', '.join(json.dumps(name) for name in [*item['julia'], *packages])
            # Pkg resolves and precompiles once; a prepared project only gains missing packages. Projects share
            # the Julia depot, so one installation runs at a time.
            with locked(folder / 'julia-packages.lock'):
                install([executable, '--startup-file=no', '--project=' + str(prefix), '-e',
                         f'import Pkg; Pkg.add([{names}]); Pkg.precompile()'])
            connection = routes.bind(tool, route='executable', executable=executable, project=str(prefix), path=path, **kept)
            installer = 'julia'
        else:
            spec = item['executable']
            executable = prefix / spec['path']
            if not executable.exists():
                if prefix.exists():
                    shutil.rmtree(prefix)
                create(prefix, item, None, path)
            if packages:
                install([conda(path), 'install', '-y', '-q', '-p', str(prefix), '-c', 'conda-forge', '--override-channels', *packages],
                        home(path) / 'conda')
            variables = {key: value.replace('{prefix}', str(prefix)) for key, value in spec.get('variables', {}).items()}
            kept['env'] = {**variables, **kept.get('env', {})}
            connection = routes.bind(tool, route='executable', executable=str(executable), path=path, **kept)
            installer = 'conda'
        earlier = (configured or {}).get('packages', [])
        managed = {**{k: v for k, v in connection.items() if k != 'tool'}, 'managed': item['environment'], 'installer': installer,
                   'packages': sorted(set(earlier) | set(item['julia'] or item['conda']) | set(packages))}
        update('tools', tool, managed, path)
    return {'tool': tool, **managed}


def state(tool, value, path=None):
    """Whether a connection's interpreter, executable or automation server is there now."""
    if value['kind'] == 'python':
        return probe(tool, value['python'], path, value.get('paths'))
    if value['kind'] == 'native':
        from . import native
        return native.probe(value)
    if value['kind'] == 'executable':
        return {'available': Path(value['executable']).is_file(), 'executable': value['executable']}
    if value['kind'] == 'automation':
        from . import routes
        return {'available': os.name == 'nt' and routes.registered(value['progid']), 'progid': value['progid']}
    return {'available': bool(shutil.which(value['command'][0]))}


def available(path=None):
    entries = []
    for tool, value in read(path)['tools'].items():
        item = {'id': tool, **value}
        try:
            item['environment'] = state(tool, value, path)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            item['environment'] = {'available': False, 'error': str(error)}
        entries.append(item)
    return entries


def page(tool=None, offset=0, limit=20, refresh=False, path=None):
    """Read connection summaries without starting every scientific interpreter."""
    if offset < 0 or not 1 <= limit <= 100: raise ValueError('Use offset >= 0 and limit from 1 to 100')
    configured = read(path)['tools']
    keys = sorted(k for k in configured if tool is None or k == tool)
    items = []
    for key in keys[offset:offset+limit]:
        connection = configured[key]
        row = {'id':key, **{k:v for k,v in connection.items() if k in ('kind','threads','concurrency','requirements','versions','managed','packages','executable','language','progid')}}
        row['state'] = 'configured'
        if refresh:
            try:
                row['environment'] = state(key, connection, path)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                row['environment'] = {'available':False, 'error':str(error)}
        items.append(row)
    return {'environments':items, 'total':len(keys), 'next_offset':offset+limit if offset+limit<len(keys) else None}
