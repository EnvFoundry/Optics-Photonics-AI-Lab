"""Dependency-free MCP stdio server. Calculations run independently in the background."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import math
import sys
import textwrap
import threading
from . import __version__, catalogue, data, environments
from . import jobs, languages, signals, composition


def schema(properties=None, required=None):
    return {'type':'object','properties':properties or {},'required':required or [],'additionalProperties':False}


def guides():
    """Topic -> Markdown file: each Skill under its name and each of its references under the file name."""
    found = {}
    for skill in sorted((catalogue.resources()/'skills').iterdir()):
        if (skill/'SKILL.md').is_file():
            found[skill.name] = skill/'SKILL.md'
    for skill in list(found.values()):
        for file in sorted((skill.parent/'references').glob('*.md')):
            found[file.stem] = file
    return found


def guide(topic):
    text = guides()[topic].read_text(encoding='utf-8')
    # Skill front matter addresses Skill loaders, not readers.
    return text.split('---', 2)[2].lstrip() if text.startswith('---') else text


def summary(topic):
    """The first sentence of a guide, as the description of its resource."""
    body = ' '.join(line for line in guide(topic).splitlines() if line.strip() and not line.startswith(('#', '|', '-', '```')))
    sentence = body.split('. ')[0].rstrip('.')
    return sentence if len(sentence) <= 240 else sentence[:237].rsplit(' ', 1)[0] + ' …'


def tool(name, description, properties=None, required=None):
    """One MCP tool: its description is written as wrapped text and sent as one paragraph."""
    return name, ' '.join(description.split()), schema(properties, required)


DOMAINS = [row[0] for row in json.loads((catalogue.resources()/'data'/'domains.en.json').read_text(encoding='utf-8'))]
# Units in which a wavelength can be given: vacuum wavelength, frequency, wavenumber or photon energy.
SPECTRAL_UNITS = [unit for kind in ('length', 'frequency', 'angular frequency', 'wavenumber', 'energy') for unit in data.UNITS[kind]]
STRING = {'type': 'string'}
BOOLEAN = {'type': 'boolean'}
OBJECT = {'type': 'object'}
POSITIVE = {'type': 'integer', 'minimum': 1}
COUNT = {'type': 'integer', 'minimum': 0}
STRINGS = {'type': 'array', 'items': {'type': 'string'}}
SECONDS = {'type': 'number', 'exclusiveMinimum': 0}
WAIT = {'type': 'number', 'minimum': 0, 'maximum': 25}
QUANTITIES = {'type': 'object', 'additionalProperties': {'type': 'integer', 'minimum': 1}}
VARIABLES = {'type': 'object', 'additionalProperties': {'type': 'string'}}

TOOLS = [
    tool('read_guide', '''
        Read operating guidance as Markdown. optical-computation: how to connect software, submit programs and read results.
        optical-research: how to choose a physical model and software. The other topics are the references those two link
        to, by link name.''',
         {'topic': {'enum': list(guides())}}, ['topic']),
    tool('software_search', '''
        Find optical software and packaged programs by physical problem, method or name, in English or Chinese; synonyms and
        abbreviations of one concept match (PSF, point spread function, 点扩散函数). Best matches first. An item names the
        packaged program that fits as program, ready for submit_calculation(tool, action); its routes (how the environment
        reaches the software); install (pip, conda or julia when a recipe installs it, manual otherwise); and in missing the
        query terms it lacks. domain keeps the software of a domain and the software with a packaged program of it.''',
         {'query': STRING, 'domain': {'enum': DOMAINS}, 'license_group': {'enum': ['open', 'paid', 'limited']},
          'offset': COUNT, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
          'language': {'enum': ['zh', 'en']}}),
    tool('software_details', '''
        Read one software: method, limits, license, routes (python, session, executable, automation, service, inside: how
        the environment reaches it, with the program contract of its language), installation recipe and the names of its
        packaged programs. program returns one packaged program: its physics and conventions, every input with default
        and meaning, results and checks; source adds its text, to adapt and submit. brief=false returns the complete
        software record and all program input definitions.''',
         {'tool': STRING, 'program': STRING, 'source': BOOLEAN, 'brief': {'type': 'boolean', 'default': True},
          'language': {'enum': ['zh', 'en']}}, ['tool']),
    tool('materials', '''
        Refractive index n and extinction coefficient k from the refractiveindex.info database (3576 pages, CC0) with
        references. material is a common name (SiO2, Si, Au, N-BK7) or a page ID shelf/book/page (main/Si/Green-2008). With
        wavelength (number or list in unit, nm by default): n and k; without: the page's range and references. dispersion
        adds the group index, group velocity dispersion (fs^2/mm) and D (ps/(nm km)). query searches page names; no
        arguments lists the common names. Other pages download once.''',
         {'material': STRING, 'query': STRING, 'dispersion': BOOLEAN,
          'wavelength': {'anyOf': [SECONDS, {'type': 'array', 'items': SECONDS, 'minItems': 1, 'maxItems': 10000}]},
          'unit': {'enum': SPECTRAL_UNITS}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}),
    tool('register_software', '''
        Register a local research library, solver or executable that is not in the published catalogue. Provide its
        identity, module/distribution names and documentation sources; then use bind_environment. pip, conda (conda-forge
        packages), python (version specifier) and platforms form an installation recipe, so that prepare_environment and
        submissions install it. Registration does not download or execute it.''',
         {'tool': STRING, 'name': STRING, 'description': STRING, 'kind': {'enum': ['python', 'command']},
          'modules': STRINGS, 'distributions': STRINGS, 'sources': STRINGS, 'pip': STRINGS, 'conda': STRINGS,
          'python': STRING, 'platforms': {'type': 'array', 'items': {'enum': list(environments.PLATFORMS)}}}, ['tool', 'name', 'description']),
    tool('registered_software', '''
        List locally registered research tools beyond the published software catalogue.'''),
    tool('software_integrations', '''
        List installed software integration packages, their programs and guides, and any load errors. With integration
        and topic, read one package guide. refresh reloads installed metadata after an install, upgrade or removal;
        it does not import scientific libraries or run their code.''',
         {'integration': STRING, 'topic': STRING, 'language': {'enum': ['en', 'zh']}, 'refresh': BOOLEAN}),
    tool('environments', '''
        Read a short page of configured software. refresh=true checks only this page’s installed programs; tool selects one
        connection.''',
         {'tool': STRING, 'offset': COUNT, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
          'refresh': BOOLEAN}),
    tool('discover_native_software', '''
        Find installed optical software without starting it: executables (MATLAB, Octave, Julia, wolframscript, Maple,
        COMSOL, Lumerical, CST, S4, ADDA, MCX, MMC, pbrt), session SDKs (COMSOL, Lumerical, OpticStudio, CST), modules
        inside installations (JCMsuite, Photon Design), Windows automation servers (FRED, CODE V, LightTools, OptiLayer) and
        service credentials (Tidy3D, by presence). Searches PATH, usual installation folders, variables such as JCMROOT,
        roots and the Windows registry. Each installation carries the arguments of bind_native_software. tool limits the
        search to one software and adds its routes.''',
         {'roots': STRINGS, 'tool': STRING}),
    tool('bind_native_software', '''
        Connect installed software by one route; without route or executable, the first installation
        discover_native_software finds. session (COMSOL, Lumerical, OpticStudio, CST): python, sdk_path, options; a
        calculation owns a session with the full native API. executable: executable; then script runs a program in the
        software's language and source a Python program that drives it with RUN. automation (Windows): progid; a Python
        program holds the server as APP. python: python and sdk_path, the folder of a module inside the installation.
        language sets the script language of an executable outside the catalogue; project is a Julia project. Details:
        read_guide native-software.''',
         {'tool': STRING, 'route': {'enum': ['session', 'executable', 'automation', 'python']}, 'executable': STRING,
          'python': STRING, 'sdk_path': STRING, 'progid': STRING, 'project': STRING,
          'language': {'enum': list(languages.LANGUAGES)}, 'options': OBJECT, 'threads': POSITIVE,
          'concurrency': POSITIVE, 'requirements': QUANTITIES, 'env': VARIABLES}, ['tool']),
    tool('prepare_environment', '''
        Install a tool from its recipe (pip, or conda-forge for software without wheels) into an environment of its own, in
        the background; returns a run ID. Submissions do this by themselves for a tool without an environment. packages adds
        requirements such as scipy or h5py to the tool's environment; python selects the base interpreter.''',
         {'tool': STRING, 'packages': STRINGS, 'python': STRING, 'request_id': STRING}, ['tool']),
    tool('bind_environment', '''
        Connect an existing scientific Python environment or an explicit native executable; installs nothing. Without tool,
        binds every catalogue or registered tool whose modules the python interpreter has, and keeps tools already
        connected.''',
         {'tool': STRING, 'python': STRING, 'command': {'type': 'array', 'items': STRING, 'minItems': 1},
          'threads': POSITIVE, 'concurrency': POSITIVE, 'cwd': STRING, 'requirements': QUANTITIES, 'env': VARIABLES}),
    tool('compute_resources', '''
        Read or update shared CPU and named resource capacities (GPU, solver license, memory slots). Requirements are
        acquired together across all runs; adapters select the actual hardware or license.''',
         {'cpu': POSITIVE, 'pools': QUANTITIES}),
    tool('submit_calculation', '''
        Start a background calculation and return its run ID; with wait (up to 25 s) also its state and, once finished, its
        RESULT, files and output folder, or its error. Give one of: action, a packaged program (software_details) with
        inputs replacing its defaults; source, a complete Python program that receives INPUTS and OUTPUT_DIR and sets
        RESULT; script, the same in the language of software bound to its executable (MATLAB, Octave, Julia, Wolfram, Maple,
        Lua, Lumerical script). sweep {input: values or {start, stop, points}} runs every combination (pairing zip: in step)
        as parallel points, tabulated by sweep_results. optimize {variables: {input: [low, high]}, minimize or maximize:
        RESULT path} searches inside one process. A tool without an environment is installed from its recipe first. run_id
        appends to a run; reuse request_id when retrying. Details: read_guide studies.''',
         {'tool': STRING, 'inputs': OBJECT, 'action': STRING, 'source': STRING, 'script': STRING, 'sweep': OBJECT,
          'pairing': {'enum': ['product', 'zip']}, 'chunk': POSITIVE, 'optimize': OBJECT, 'run_id': STRING,
          'name': STRING, 'request_id': STRING, 'timeout': SECONDS, 'wait': WAIT}, ['tool']),
    tool('sweep_results', '''
        Tabulate the points of a sweep: each point's swept inputs, state and the RESULT values named in values (dotted paths
        such as checks.energy_error or R.0). minimize or maximize one value to get the best point. save writes
        sweep.npz/.json in the run folder, gridded over the swept inputs for a full product, for read_array.''',
         {'run_id': STRING, 'values': STRINGS, 'minimize': STRING, 'maximize': STRING, 'save': BOOLEAN, 'offset': COUNT,
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000}}, ['run_id']),
    tool('sample_episodes', '''
        Collect complete episodes for learning in parallel worker processes. source defines make_env() with Gymnasium-style
        reset/step/close and make_policy(seed) returning an action function; task and policy need id and version. Each
        worker writes samples/trajectory.jsonl, samples/index.jsonl (one line per completed episode) and typed arrays.
        continue_from lists earlier runs whose completed episodes are not collected again. Details: read_guide episodes.''',
         {'tool': STRING, 'source': STRING, 'task': OBJECT, 'policy': OBJECT, 'episodes': POSITIVE, 'workers': POSITIVE,
          'max_steps': POSITIVE, 'seed': COUNT, 'inputs': OBJECT, 'options': OBJECT, 'continue_from': STRINGS,
          'on_error': {'enum': ['stop', 'continue']}, 'request_id': STRING, 'timeout': SECONDS}, ['tool', 'source', 'task', 'policy', 'episodes']),
    tool('submit_workflow', '''
        Submit dependent or parallel calculation steps in one request. A step has id, tool, one of action, source or script,
        inputs, and optionally after, bindings, requirements and timeout. A binding {input: {step, path: [keys]}} copies a
        value from an earlier step's result; {input: {step, file: "relative/path"}} gives the location of a file or folder
        it wrote. run_id appends steps to an existing run.''',
         {'steps': {'type': 'array', 'items': OBJECT, 'minItems': 1}, 'name': STRING, 'request_id': STRING,
          'run_id': STRING}, ['steps']),
    tool('run_status', '''
        Read compact execution state. wait (0–25 seconds) waits for completion without repeated calls. Results stay in files
        by default; include_results returns step values. Calculations continue after disconnection.''',
         {'run_id': STRING, 'offset': COUNT, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
          'include_results': BOOLEAN, 'wait': WAIT}, ['run_id']),
    tool('cancel_run', '''
        Cancel a research run and its active calculations.''',
         {'run_id': STRING}, ['run_id']),
    tool('result_files', '''
        List files written so far by a research run, including running calculations. prefix such as "solve/attempt-1/"
        limits the listing to one step or folder. NPY/NPZ entries include dtype and shape, and the quantity, unit and axis
        names a program saved with them.''',
         {'run_id': STRING, 'prefix': STRING, 'offset': COUNT,
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500}}, ['run_id']),
    tool('read_array', '''
        Read part of an NPY array or NPZ member without loading the file. index has one entry per leading axis: an integer
        selects a position and drops the axis, [start, stop, step] keeps a range, null the whole axis. Beyond max_elements
        long axes are thinned uniformly; selection reports the steps. Complex data returns real and imag. A described array
        adds description (quantity, unit, conventions), axes (coordinates of the returned values) and at (positions fixed by
        integers). image draws a one- or two-dimensional selection as a PNG (auto, line or map) and returns the extremes
        with their positions instead of the values; part selects abs, real, imag, phase or power; scale log draws log10.''',
         {'run_id': STRING, 'file': STRING, 'member': STRING,
          'index': {'type': 'array', 'items': {'anyOf': [{'type': 'integer'}, {'type': 'null'}, {'type': 'array', 'items': {'type': ['integer', 'null']}, 'minItems': 1, 'maxItems': 3}]}},
          'max_elements': {'type': 'integer', 'minimum': 1, 'maximum': 100000},
          'image': {'enum': ['auto', 'line', 'map']}, 'part': {'enum': ['abs', 'real', 'imag', 'phase', 'power']},
          'scale': {'enum': ['linear', 'log']}}, ['run_id', 'file']),
    tool('download_result', '''
        Copy a result file to a new destination on the MCP host computer. From another computer, read its content with
        read_result or read_array.''',
         {'run_id': STRING, 'file': STRING, 'destination': STRING}, ['run_id', 'file', 'destination']),
    tool('read_result', '''
        Read a bounded byte range of a result or log. Text returns UTF-8; binary returns base64. Use read_array for NPY/NPZ
        data.''',
         {'run_id': STRING, 'file': STRING, 'offset': COUNT,
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 65536}}, ['run_id', 'file']),
    tool('list_runs', '''
        Find previous and active research runs by name, run ID, request ID or software. tool and state filter the results.
        Pass next_before as before for a stable next page while other runs arrive; offset remains available. Returns
        compact summaries, software names and step counts; run_status gives details.''',
         {'search': STRING, 'tool': STRING, 'before': STRING,
          'state': {'enum': sorted(jobs.TERMINAL | {'queued', 'running'})}, 'offset': COUNT,
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}),
    tool('resume_run', '''
        Continue unfinished steps of a stopped run, keeping completed work and previous output files. Python programs
        can restore CHECKPOINT state; built-in optimization and grouped sweeps reuse saved evaluations. Read the error
        and progress first; read_guide continuation explains the program contract.''',
         {'run_id': STRING}, ['run_id']),
    tool('environment_installed', 'Read installed software.'),
    tool('environment_plan', 'Resolve selected dependencies.',
         {'selections': STRINGS}, ['selections']),
    tool('environment_prepare', 'Install selected software.',
         {'selections': STRINGS, 'python': STRING, 'packages': STRINGS, 'request_id': STRING}, ['selections']),
    tool('environment_remove', 'Uninstall selected software.',
         {'selections': STRINGS}, ['selections']),
]


# Hints describe possible effects, not permission to execute. run_status can restart a queued worker;
# submissions and resumes run user programs, while bindings and resource settings replace configuration.
READ_ONLY = {'environment_installed', 'environment_plan', 'read_guide', 'software_search', 'software_details', 'materials', 'registered_software', 'software_integrations', 'environments',
             'discover_native_software', 'result_files', 'read_result', 'read_array', 'list_runs'}
EXECUTING = {'environment_prepare', 'prepare_environment', 'submit_calculation', 'submit_workflow', 'sample_episodes', 'resume_run', 'run_status'}
SETTINGS = {'compute_resources', 'bind_environment', 'bind_native_software', 'register_software'}
DESTRUCTIVE = EXECUTING | SETTINGS | {'cancel_run', 'sweep_results', 'environment_remove'}
OPEN_WORLD = EXECUTING | {'materials'}


def listed():
    """The tools as tools/list sends them."""
    return [{'name': name, 'description': description, 'inputSchema': spec,
             **({'execution': {'taskSupport': 'optional'}} if name == 'environment_prepare' else {}),
             'annotations': {'readOnlyHint': name in READ_ONLY, 'destructiveHint': name in DESTRUCTIVE,
                             'idempotentHint': name in READ_ONLY or name in SETTINGS or name == 'cancel_run',
                             'openWorldHint': name in OPEN_WORLD}}
            for name, description, spec in TOOLS]


# Starting points a host offers to its user, each a complete instruction for one kind of work in this environment.
PROMPTS = {
    'calculate': ('Compute an optical quantity with the packaged program or the software that fits.', {
        'problem': 'what to compute, with the known values and their units'}, '''
        Compute this in the optics research environment: {problem}

        Find the software and the packaged program with software_search and read the program's inputs, physics and checks
        with software_details(tool, program=NAME). Submit it with submit_calculation and wait; when no packaged program
        fits, adapt the closest one or write a complete program for the software's own API. Read the checks of the result
        before trusting the numbers, and look at the main curve or map with read_array(image="auto"). Report the model
        and its assumptions, every input with its unit, the result, the checks and the run ID.'''),
    'study': ('Vary parameters of a calculation: a sweep, its table and refinement, or an optimization.', {
        'problem': 'the calculation and the quantity of interest',
        'parameters': 'the parameters to vary, with ranges and units'}, '''
        Study how the result depends on the parameters. Calculation: {problem}. Parameters: {parameters}

        Run one point first and read its checks. Then submit_calculation with sweep for a map over the ranges
        (sweep_results tabulates it and names the best point) or with optimize for the best value within bounds;
        read_guide studies explains both. Refine around what the first sweep shows instead of a finer grid everywhere.
        Report the table or the optimum with units, how the checks behave across the range, and the run IDs.'''),
    'reproduce': ('Reproduce a published result and compare the numbers.', {
        'source': 'the paper, figure, table or textbook result to reproduce'}, '''
        Reproduce this published result in the optics research environment: {source}

        State the physical model, geometry, materials and every parameter the source gives, and which ones had to be
        assumed. Choose the software with software_search (read_guide optical-research helps to choose the model).
        Preserve the source's material model; use materials for missing optical constants and name that assumption.
        Compare with the published values in the same units and conventions. Where discretization affects the result,
        refine it enough to distinguish numerical error from a physical difference. Report agreement and differences.'''),
    'connect': ('Connect scientific software installed on this computer and confirm that it runs.', {
        'software': 'the software to connect, by name'}, '''
        Connect this installed software to the optics research environment: {software}

        Find its ID and routes with software_search and software_details. For an existing Python environment, use
        bind_environment; for an installed executable or SDK, use discover_native_software then bind_native_software.
        prepare_environment installs a missing tool from its recipe. Use the selected installation and available
        license or device capacities; ask only for information that discovery and the project do not supply.
        Run a short calculation through the chosen route, then report the version and connection.
        read_guide native-software describes the routes.'''),
    'measure': ('Drive a laboratory instrument and record a measurement.', {
        'task': 'the instrument, what to set and what to read, with ranges and units'}, '''
        Carry out this measurement with the optics research environment: {task}

        Start with read_guide lab. Find the instrument library with software_search (domain lab) and use that library's
        discovery method; pyvisa/sweep with resource "?" applies to VISA instruments. Reuse the experiment's controller
        and device ownership. Execute within the equipment, actions and ranges already authorized; ask for missing
        settings or a proposed change outside that scope. Use the existing named resource pools when several tools
        share a device. Save settings, times and raw readings as they arrive. Apply the agreed finish or restoration
        procedure, retain partial data after failures, and report the measurement with units and its saved files.'''),
    'continue': ('Pick up earlier work: what ran, what it found, what is unfinished.', {}, '''
        Continue the earlier work in the optics research environment.

        list_runs shows the runs with their states; run_status gives the steps of one, result_files and read_result its
        outputs. Summarize what each recent run computed and found, which runs are still running or stopped (resume_run
        reruns unfinished steps and keeps finished ones), and propose the next step. Do not rerun what has finished.'''),
}


def prompts():
    """The prompts as prompts/list sends them."""
    return [{'name': name, 'description': about, 'arguments': [{'name': key, 'description': meaning, 'required': True}
                                                                for key, meaning in arguments.items()]}
            for name, (about, arguments, _) in PROMPTS.items()]


def prompt(name, values):
    """The message of a prompt with its arguments filled in."""
    if name not in PROMPTS:
        raise ValueError('Unknown prompt: ' + str(name) + '. Prompts: ' + ', '.join(PROMPTS))
    about, arguments, text = PROMPTS[name]
    values = values if isinstance(values, dict) else {}
    missing = [key for key in arguments if values.get(key) is None or values.get(key) == ''
               or isinstance(values.get(key), str) and not values[key].strip()]
    if missing:
        raise ValueError(f"{name} needs: {', '.join(missing)}")
    invalid = [key for key in arguments if not isinstance(values[key], str)]
    if invalid:
        raise ValueError(f"{name} needs text for: {', '.join(invalid)}")
    text = textwrap.dedent(text).strip().format_map({key: values[key].strip() for key in arguments})
    return {'description': about, 'messages': [{'role': 'user', 'content': {'type': 'text', 'text': text}}]}


def invoke(name, args, cancellation=None):
    if name == 'environment_installed': return composition.installed()
    if name == 'environment_plan': return composition.plan(**args)
    if name == 'environment_prepare': return composition.prepare(**args)
    if name == 'environment_remove': return composition.remove(**args)
    if name == 'read_guide': return guide(args['topic'])
    if name == 'software_search': return catalogue.search(**args)
    if name == 'materials':
        from . import materials
        return materials.lookup(**args)
    if name == 'software_details': return environments.details(**{'source': False, 'brief': True, **args})
    if name == 'register_software': return environments.register(**args)
    if name == 'registered_software': return {'software': environments.registered()}
    if name == 'software_integrations':
        from . import integrations
        if args.get('integration') is not None or args.get('topic') is not None:
            if not args.get('integration') or not args.get('topic'):
                raise ValueError('Read a package guide with both integration and topic; omit both to list packages')
            if args.get('refresh'): integrations.listing(refresh=True)
            return integrations.guide(args['integration'], args['topic'], language=args.get('language', 'en'))
        return integrations.listing(refresh=args.get('refresh', False))
    if name == 'environments': return environments.page(**args)
    if name in ('discover_native_software','bind_native_software'):
        from . import native
        return native.discover(**args) if name == 'discover_native_software' else native.bind(**args)
    if name == 'bind_environment': return environments.bind(**args)
    if name == 'prepare_environment': return jobs.prepare(**args)
    if name == 'compute_resources': return jobs.resources(**args)
    if name == 'submit_calculation':
        from . import studies
        return studies.submit(**{'name': 'Optics research', **args}, cancel=cancellation)
    if name == 'sweep_results':
        from . import studies
        return studies.table(**args)
    if name == 'sample_episodes':
        from . import sampling
        return sampling.submit(**args)
    if name == 'submit_workflow': return jobs.submit(**args)
    if name == 'run_status': return jobs.status(**{'include_results':False, **args}, cancel=cancellation)
    if name == 'cancel_run': return jobs.cancel(**args)
    if name == 'result_files': return jobs.files(**args)
    if name == 'download_result': return jobs.download(**args)
    if name == 'read_result': return jobs.read_result(**args)
    if name == 'read_array': return jobs.read_array(**args)
    if name == 'resume_run': return jobs.resume(**args)
    if name == 'list_runs': return jobs.list_runs(**args)
    raise ValueError('Unknown tool: '+name)


class Connection:
    """One stdio session: requests are read in order and answered as each completes.

    Tool calls run on a bounded thread pool; status waits have their own
    threads, so no number of waits delays a query or cancel_run sent after
    them. Each response is written as one line. MCP request cancellation ends
    the request's wait; cancel_run stops a calculation. Calculations already
    accepted continue after disconnection.
    """
    WORKERS = 16   # tool calls executing at once, waits excluded
    PENDING = 256  # requests accepted and not yet answered

    def __init__(self, outgoing):
        self.outgoing, self.lock = outgoing, threading.Lock()
        self.active, self.withdrawn, self.state = {}, set(), threading.Lock()

    def send(self, message):
        text = json.dumps(message, ensure_ascii=False, allow_nan=False) + '\n'
        with self.lock:
            try:
                self.outgoing.write(text); self.outgoing.flush()
            except (OSError, ValueError):
                pass  # The client has gone; accepted calculations continue.

    def cancel(self, request_id):
        # The host no longer expects this reply (MCP notifications/cancelled).
        if not valid_id(request_id): return
        with self.state:
            cancellation = self.active.get(key(request_id))
            if cancellation: self.withdrawn.add(key(request_id))
        if cancellation: cancellation.set()

    def accept(self, request_id):
        with self.state:
            if len(self.active) >= self.PENDING:
                raise RuntimeError(f'{self.PENDING} requests are already in progress on this connection')
            if key(request_id) in self.active:
                raise ValueError('This request ID is already in progress')
            cancellation = self.active[key(request_id)] = signals.Cancellation()
            return cancellation

    def call(self, request_id, params, cancellation, method='tools/call'):
        try:
            with self.state:
                if key(request_id) in self.withdrawn: return
            result = (composition.task_rpc(method, params['taskId'], cancellation)
                      if method.startswith('tasks/') else tool_result(params, cancellation))
            with self.state:
                withdrawn = key(request_id) in self.withdrawn
            if not withdrawn:
                self.send({'jsonrpc':'2.0','id':request_id,'result':result})
        except Exception as error:
            self.send({'jsonrpc':'2.0','id':request_id,'error':{'code':-32602 if isinstance(error, ValueError) else -32603,'message':str(error)}})
        finally:
            with self.state:
                self.active.pop(key(request_id), None); self.withdrawn.discard(key(request_id))

    def close(self):
        # End of input: waits return their current state; replies are still written.
        with self.state:
            pending = list(self.active.values())
        for cancellation in pending: cancellation.set()


def valid_id(request_id):
    return type(request_id) in (str, int) or type(request_id) is float and math.isfinite(request_id)


def key(request_id):
    # JSON-RPC distinguishes 1 from "1".
    return (type(request_id).__name__, request_id)


def waiting(params):
    """A run_status or submit_calculation call that waits for completion; it occupies no worker."""
    if not isinstance(params, dict) or params.get('name') not in ('run_status', 'submit_calculation'): return False
    arguments = params.get('arguments')
    return isinstance(arguments, dict) and bool(arguments.get('wait'))


def conform(value, spec, name):
    """Return an argument value that satisfies its schema, or raise ValueError naming what is expected.

    Numbers and booleans sent as text are accepted; some hosts send every argument as a string.
    """
    if 'anyOf' in spec:
        for option in spec['anyOf']:
            try: return conform(value, option, name)
            except ValueError: pass
        raise ValueError(name+' has an unsupported form')
    if 'enum' in spec:
        if value not in spec['enum']: raise ValueError(name+' must be one of: '+', '.join(map(str, spec['enum'])))
        return value
    kinds = spec.get('type')
    kinds = kinds if isinstance(kinds, list) else [kinds]
    if value is None and 'null' in kinds: return None
    kind = next((k for k in kinds if k != 'null'), None)
    if kinds == ['null']: raise ValueError(name+' must be null')
    if kind in ('integer', 'number'):
        low, high = spec.get('minimum', spec.get('exclusiveMinimum')), spec.get('maximum')
        expected = name+(' must be an integer' if kind == 'integer' else ' must be a number')+ \
            (f' from {low} to {high}' if low is not None and high is not None else f' above {low}' if 'exclusiveMinimum' in spec
             else f' of at least {low}' if low is not None else '')
        if isinstance(value, str):
            try: value = float(value) if kind == 'number' else int(value)
            except ValueError: raise ValueError(expected) from None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value): raise ValueError(expected)
        if kind == 'integer':
            if value != int(value): raise ValueError(expected)
            value = int(value)
        if 'minimum' in spec and value < spec['minimum'] or 'exclusiveMinimum' in spec and value <= spec['exclusiveMinimum'] \
           or high is not None and value > high: raise ValueError(expected)
    elif kind == 'boolean':
        if isinstance(value, str) and value.lower() in ('true', 'false'): value = value.lower() == 'true'
        if not isinstance(value, bool): raise ValueError(name+' must be true or false')
    elif kind == 'string':
        if not isinstance(value, str): raise ValueError(name+' must be text')
    elif kind == 'array':
        if not isinstance(value, list): raise ValueError(name+' must be a list')
        if len(value) < spec.get('minItems', 0) or len(value) > spec.get('maxItems', len(value)):
            raise ValueError(f"{name} must have {spec.get('minItems', 0)} to {spec.get('maxItems', 'any number of')} items")
        if 'items' in spec: value = [conform(item, spec['items'], f'{name}[{i}]') for i, item in enumerate(value)]
    elif kind == 'object':
        if not isinstance(value, dict): raise ValueError(name+' must be an object')
        if isinstance(spec.get('additionalProperties'), dict):
            value = {key: conform(item, spec['additionalProperties'], f'{name}.{key}') for key, item in value.items()}
    return value


def arguments(name, args, spec):
    """Tool arguments matched against the tool's input schema, with messages an agent can act on."""
    if not isinstance(args, dict): raise ValueError('arguments must be an object')
    unknown, missing = sorted(set(args)-set(spec['properties'])), sorted(set(spec['required'])-set(args))
    if unknown: raise ValueError(f"{name} has no argument {', '.join(unknown)}; it takes: {', '.join(spec['properties']) or 'no arguments'}")
    if missing: raise ValueError(f"{name} needs: {', '.join(missing)}")
    return {key: conform(value, spec['properties'][key], key) for key, value in args.items()}


def tool_result(params, cancellation=None):
    try:
        name = params['name']; args = params.get('arguments') or {}
        spec = next((s for n,d,s in TOOLS if n == name), None)
        if spec is None: raise ValueError('Unknown tool: '+name+'. Tools: '+', '.join(n for n,d,s in TOOLS))
        if 'task' in params and name != 'environment_prepare':
            raise ValueError('This tool does not support tasks')
        value = invoke(name, arguments(name, args, spec), cancellation)
        if 'task' in params:
            return {'task': composition.task(value['run_id'], starting=True)}
        if isinstance(value, str):  # Markdown guidance is returned as written
            return {'content':[{'type':'text','text':value}], 'structuredContent':{'text':value}, 'isError':False}
        picture = value.pop('_image', None) if isinstance(value, dict) else None
        content = [{'type':'text','text':json.dumps(value,ensure_ascii=False,allow_nan=False)}]
        if picture is not None:
            content.insert(0, {'type':'image','data':base64.b64encode(picture).decode('ascii'),'mimeType':'image/png'})
        return {'content':content, 'structuredContent':value, 'isError':False}
    except Exception as error:
        return {'content':[{'type':'text','text':str(error) or type(error).__name__}], 'isError':True}


def respond(method, params):
    if method == 'initialize':
        proposed = params.get('protocolVersion')
        return {'protocolVersion':proposed if proposed in SUPPORTED else '2025-11-25',
                'serverInfo':{'name':'optics-photonics','version':__version__},
                'capabilities':{'tools':{'listChanged':False},'resources':{},'prompts':{},
                                'tasks': {'requests': {'tools': {'call': {}}}, 'cancel': {}}},
                'instructions':INSTRUCTIONS}
    if method == 'ping': return {}
    if method == 'tools/list':
        return {'tools': listed()}
    if method == 'resources/list':
        return {'resources':[{'uri':'optics://guides/'+name,'name':name,'description':summary(name),'mimeType':'text/markdown'}
                             for name in guides()]}
    if method == 'resources/read':
        uri = params.get('uri','')
        name = uri.removeprefix('optics://guides/')
        if uri != 'optics://guides/'+name or name not in guides():
            raise ValueError('Unknown optical guide')
        return {'contents':[{'uri':uri,'mimeType':'text/markdown','text':guide(name)}]}
    if method == 'prompts/list':
        return {'prompts': prompts()}
    if method == 'prompts/get':
        return prompt(params.get('name'), params.get('arguments'))
    return None


SUPPORTED = {'2025-11-25','2025-06-18','2025-03-26','2024-11-05'}
INSTRUCTIONS = ' '.join('''
    Optics and photonics research environment: software for modelling, simulation, computation and laboratory instruments,
    run as background calculations that continue after disconnection.
    Find: software_search by physical problem, in English or Chinese, names the software and the packaged program that fits;
    software_details lists its programs and routes; request a program for its inputs and physics, and source when
    adapting it. materials gives measured refractive indices.
    Run: submit_calculation with action (a packaged program), source (a Python program that receives INPUTS and OUTPUT_DIR
    and sets RESULT) or script (the same in the language of installed software). The first run installs the software from its
    recipe; wait returns a short result in the same call; sweep and optimize study parameters; submit_workflow chains steps.
    Read: run_status with wait, then result_files, read_result, read_array and sweep_results; read the checks of a result
    before trusting it. list_runs searches earlier runs by name, request ID or software.
    Installed commercial or executable software: discover_native_software, then bind_native_software.
    software_integrations lists independently installed programs and their on-demand guides.
    Long programs save explicit state with CHECKPOINT; read_guide continuation explains recovery through resume_run.
    read_guide optical-computation explains execution, read_guide optical-research the choice of model and software.
    Licenses remain those of the upstream software.'''.split())


def serve(input_stream=None, output_stream=None):
    incoming = input_stream or sys.stdin
    connection = Connection(output_stream or sys.stdout)
    with ThreadPoolExecutor(Connection.WORKERS, thread_name_prefix='optics-mcp') as pool, \
         ThreadPoolExecutor(Connection.PENDING, thread_name_prefix='optics-mcp-wait') as waits:
        try:
            for line in incoming:
                if not line.strip(): continue
                id = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
                        raise ValueError('Expected a JSON-RPC 2.0 request')
                    method = request.get('method'); params = request.get('params') or {}
                    if 'id' not in request:
                        if method == 'notifications/cancelled' and isinstance(params, dict):
                            connection.cancel(params.get('requestId'))
                        continue
                    if not valid_id(request['id']):
                        raise ValueError('A request ID is a string or a number')
                    id = request['id']
                    if method == 'tools/call':
                        lane = waits if waiting(params) else pool
                        lane.submit(connection.call, id, params, connection.accept(id))
                        continue
                    if method in {'tasks/get', 'tasks/result', 'tasks/cancel'}:
                        lane = pool if method == 'tasks/get' else waits
                        lane.submit(connection.call, id, params, connection.accept(id), method)
                        continue
                    result = respond(method, params)
                    if result is None:
                        connection.send({'jsonrpc':'2.0','id':id,'error':{'code':-32601,'message':'Method not found'}}); continue
                    connection.send({'jsonrpc':'2.0','id':id,'result':result})
                except json.JSONDecodeError:
                    connection.send({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON'}})
                except Exception as error:
                    connection.send({'jsonrpc':'2.0','id':id,'error':{'code':-32600,'message':str(error)}})
        finally:
            # End waits promptly; submissions already accepted finish registering.
            connection.close()
