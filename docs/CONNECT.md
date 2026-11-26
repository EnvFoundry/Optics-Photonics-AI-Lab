# Optics & Photonics research tools

An independent package for optical software, native research programs, background calculations, MCP, Skills and plugins. Software selection, model construction, solving, optimization and results share one interface.

## Install and calculate

The connector supports Python 3.10+ on Windows, macOS and Linux. Each solver uses its own supported Python version, operating system and license. Different tools may use different environments.

```sh
python -m pip install "https://github.com/Cherish0925/Optics-Photonics-AI-Lab/releases/download/optics-v0.11.1/optics_photonics-0.11.1-py3-none-any.whl"
optics-photonics info tmm
optics-photonics prepare tmm
optics-photonics run tmm spectrum --inputs "@inputs.json"
```

Use these inputs:

```json
{"n":[1,1.45,1.52],"thickness_nm":[100],"wavelength_nm":[450,550,650]}
```

The result includes the output directory, scientific result and software versions; the spectrum is saved as `spectrum.npz` with units and axes, and as CSV. `info tmm` lists the packaged programs of a tool with each input's default and meaning, and `info tmm --program spectrum` prints a program's source, which can be edited and submitted with `--program`. Inputs not given keep their defaults; an unknown input name is rejected. A release wheel can be installed without Git. Every command also works through `python -m optics_photonics`.

## Environments and complete programs

```sh
optics-photonics bind tmm --python "/path/to/scientific/python" --threads 1 --concurrency 4
optics-photonics environments
optics-photonics resources --cpu 8
optics-photonics run tmm --program research.py --inputs "@inputs.json"
```

`prepare` installs a tool from its recipe into an environment of its own under `OPTICS_HOME/environments`. `bind` reuses an existing interpreter; `bind --python PATH` without a tool connects every catalogue or registered tool that interpreter provides and keeps tools already connected. `threads` sets common numerical-library thread limits; `concurrency` limits simultaneous processes for one software. Total CPU capacity is shared by background runs. Configure MPI, GPUs, vendor licenses and cluster queues in the solver's native launcher.

### Installation recipes

50 open tools carry a recipe in [connections.json](../data/connections.json); `software_search` shows each tool's route as `install`: `pip`, `conda` or `manual`.

- **pip.** Pinned requirements, including a fixed commit for tools published only on GitHub (laserfun, PtyPy, py-fmas). With [uv](https://docs.astral.sh/uv/) on the PATH the environment is created by uv, which also provides the Python version a recipe states (PoPPy and GDSFactory need 3.12, Strawberry Fields below 3.12); without uv the connector's own Python and pip are used.
- **conda.** Software without usable wheels comes from conda-forge: Meep and MPB (one shared environment; no Windows build), FEniCSx, Gmsh and HoloPy. The installer is `OPTICS_CONDA`, or micromamba, mamba or conda on the PATH; otherwise the official micromamba release for this platform is downloaded once into `OPTICS_HOME/bin` and checked against its published SHA-256.
- **platforms.** A recipe lists the conda platform names it installs on when wheels do not cover all of them, for example Raysect only on `linux-64`. Elsewhere the error states the platforms.
- **manual.** Commercial software and tools whose installation needs a compiler or a vendor installer (Lumerical, COMSOL, OpticStudio, CST, JCMsuite, openEMS, PyNX, SMUTHI) are installed with their own instructions and then bound. `installation_note` records what to watch for, such as the unrelated PyPI packages named `meep` and `pynx`.

A submission for a tool without an environment installs it first: the run gains a step `prepare.TOOL`, and the calculation starts when it succeeds. An environment is created once; preparing again only adds what is missing, and two runs preparing the same environment wait for each other. `prepare TOOL --package scipy` (MCP: `packages`) adds requirements to a prepared environment and keeps its other settings. A newly prepared tool runs one thread per process and as many processes as the CPU capacity allows; `bind_environment` changes both. Installed versions, including conda-forge package versions, are recorded in the binding. [scripts/check_recipes.py](../scripts/check_recipes.py) prepares every recipe for this platform in a scratch home and reports each result; with `--programs` it then runs every packaged program of each tool with its defaults and reports its checks.

A Python program receives `INPUTS` and `OUTPUT_DIR`, and sets `RESULT` to compact JSON. It can use the complete upstream API, retain a live solver session and perform arbitrary optimization or iteration. Save arrays, meshes and models under OUTPUT_DIR. Native program examples are in [examples/programs](../examples/programs).

A native executable reads JSON from `OPTICS_REQUEST_FILE` or stdin, writes files under `OPTICS_OUTPUT_DIR`, and writes its JSON result to `OPTICS_RESULT_FILE`. `bind --command-json` accepts an executable/argument array with optional `{request}`, `{output}`, `{result}` placeholders. C, C++, C#, Java, Julia and commercial batch tools keep their native implementation.

## Background runs and dependencies

```sh
optics-photonics submit tmm --program research.py --inputs "@inputs.json" --request-id study-001
optics-photonics runs
optics-photonics status RUN_ID
optics-photonics files RUN_ID
optics-photonics read RUN_ID calculate/attempt-1/result.json
optics-photonics download RUN_ID calculate/attempt-1/spectrum.csv spectrum.csv
optics-photonics array RUN_ID calculate/attempt-1/field.npz --member field --index "[[0, null, 4], 128]"
optics-photonics array RUN_ID calculate/attempt-1/psf.npz --member psf --image psf.png --scale log
```

Submission returns immediately. The independent worker continues when the submitting CLI or MCP process exits. Reuse `request-id` for retries of the same submission. Written files can be read while the run is active. `files --prefix STEP/` lists one step, and `array` returns part of an NPY or NPZ array without loading the file; `--image FILE.png` draws that part as lines or a map with axes and a color bar (`--part phase` for complex fields, `--scale log` for decades).

A workflow is an array of steps with `id`, `tool`, `inputs`, `action` or `source`, optional `after`, `bindings` and `timeout`. Independent steps run within resource limits. A binding such as `{"value":{"step":"solve","path":["result","R"]}}` copies a predecessor result into the input named `value` and adds the dependency. For large data, `{"field":{"step":"solve","file":"field.npz"}}` sets the input `field` to the location of a file or folder that step wrote; the reference stays relative to the run and is resolved on the computing host when the step starts.

```sh
optics-photonics submit-workflow workflow.json --request-id study-002
optics-photonics submit-workflow next-steps.json --run-id RUN_ID
optics-photonics cancel RUN_ID
optics-photonics resume RUN_ID
```

Append preserves the research run. Resume retains successful steps and reruns unfinished steps into separate attempt directories. Provider-specific cloud job IDs and session continuation belong in the native research program. Complete examples appear in the [Chinese guide](CONNECT.zh-CN.md).

### Sweeps and optimization

```sh
optics-photonics submit tmm spectrum --inputs '{"n":[1,1.38,1.52],"wavelength_nm":[550]}' --sweep '{"thickness_nm.0":{"start":50,"stop":150,"points":21},"n.1":[1.30,1.38,1.46]}'
optics-photonics sweep RUN_ID --value first.1 --minimize first.1 --save
optics-photonics submit tmm spectrum --run-id RUN_ID --inputs '{"n":[1,1.30,1.52],"wavelength_nm":[550]}' --sweep '{"thickness_nm.0":{"start":100,"stop":110,"points":21}}'
optics-photonics run tmm spectrum --inputs "@coating.json" --optimize '{"variables":{"thickness_nm.0":[0,300],"thickness_nm.1":[0,300]},"minimize":"mean.R"}'
```

A sweep computes its points in parallel, several per process for short programs; a dotted name such as `thickness_nm.0` sets one element of a list input. `sweep` tabulates the swept inputs and chosen result values of every point, finds the best one and with `--save` writes `sweep.npz` gridded over the swept inputs. `--run-id` appends a finer sweep to the same run. `--optimize` searches the variables within their bounds inside one process, by differential evolution with a Nelder–Mead polish or by Nelder–Mead alone. On completion, the best evaluation's files are written to `best/`; while running or stopped, its checkpoint identifies the current best files ([parameter studies](../skills/optical-computation/references/studies.md)).

## Install from a marketplace

| Host | Add source and install |
| --- | --- |
| Agentic Research OS | **Extensions → Plugins → Add from link**: `https://github.com/Cherish0925/Optics-Photonics-AI-Lab/tree/main/connect/agentic-research-os`. Install `optics-photonics`, then activate the returned revision. |
| Codex | `codex plugin marketplace add Cherish0925/Optics-Photonics-AI-Lab`, then `codex plugin add optics-photonics@optics-photonics`. |
| Claude Code | `claude plugin marketplace add Cherish0925/Optics-Photonics-AI-Lab`, then `claude plugin install optics-photonics@optics-photonics`. |

The Research OS package includes the connector, catalogue and Skills and uses
the host's Python. Codex/Claude marketplace plugins and the DeepSeek bundle use
[uv](https://docs.astral.sh/uv/getting-started/installation/) to select Python 3.10+;
it downloads an interpreter only when needed. Install uv once on the execution host.
With an existing Python, generate a host package below to use it directly.
Codex resolves the plugin working directory; Claude expands its own plugin path variable.
Scientific software is prepared separately through `prepare_environment` or
connected through `bind_environment` / `bind_native_software`.

Update the source in Research OS and install its offered revision. Codex can
refresh with `codex plugin marketplace upgrade optics-photonics` and install the
updated plugin; Claude uses `claude plugin marketplace update optics-photonics`
followed by `claude plugin update optics-photonics@optics-photonics`. New connections
load the updated plugin; computation environments and saved research remain in
`OPTICS_HOME`. Already submitted jobs continue with their launched worker.

These are project-published GitHub marketplaces. The separate reviewed public
directories operated by OpenAI and Anthropic have their own submission process.

## Agent hosts

```sh
optics-photonics connect agentic-research-os --output optics-plugin
```

| Host | Package and activation |
| --- | --- |
| Agentic Research OS | Extract the release ZIP, install the directory containing `plugin.json`, activate the installed revision. Uses `${RESEARCH_PYTHON}`. |
| Codex | `connect codex` generates a native plugin and MCP registration command. |
| Claude Code | `connect claude`; use `claude --plugin-dir PATH` or the generated MCP command. |
| DeepSeek Harness | `connect deepseek`; use `dsh web --patch PATH/optics.cordis.yml`. |
| Qiushi | Research OS editions use its native package; other editions use `connect qiushi` for MCP or command access. |
| Other MCP hosts | `connect mcp`; register the stdio command in the host's format. |

MCP provides software search, material refractive indices (`materials`, from the refractiveindex.info database), environment preparation/binding, resource settings, submission, history, status, cancellation, resumption and result access. `prepare_environment` installs a tool's recipe in the background and returns a run ID; successful installation registers the environment. A submission for a tool without an environment does this itself. Two Skills and their references supply operating and scientific guidance on demand; `read_guide` returns any of them to a host that does not load Skills.

MCP Tasks are optional for `environment_prepare`, `prepare_environment`,
`submit_calculation`, `submit_workflow`, `sample_episodes` and `resume_run`.
With `task: {}`, the existing run ID is the Task ID; `tasks/get`, `tasks/result`
and `tasks/cancel` observe or control that same work. Environment preparation
keeps its ready/pending result; calculation tasks return the final run and its
results. Ordinary calls retain their existing responses, and `run_status`
remains a bounded immediate query. Keep the execution container alive while
its calculations run; a detached worker does not survive container termination.

Computations run on the MCP host. SSH can launch the stdio server on a workstation with the scientific software: `ssh -T USER@HOST /path/to/python -m optics_photonics mcp`. `read_result` returns bounded text/binary contents and `read_array` part of an NPY/NPZ array. `download_result` copies to a path on the MCP host. The TypeScript client's `download` transfers a file's content through the connection; SSH/SFTP remains an option for very large remote files.

Use a persistent computing host for long research runs. A detached worker survives the submitting process, but a host that disposes of the entire container or process group also stops its workers. For such Agent environments, connect to the scientific workstation over SSH rather than starting the calculation inside a disposable tool execution.

Install a new wheel or tag and generate a new plugin directory to update. Configuration, scientific environments and research records remain under `OPTICS_HOME` or `~/.optics-photonics`.

## Software and extension model

The bilingual catalogue separates commercial, open-source and restricted tools. Each connection retains primary interface documentation and upstream MCP/Skill sources. The interactive guide filters tools with packaged programs. `software_details` gives native APIs, installation recipes and each packaged program's physics, inputs and checks.

Extend software through a connection record, environment and native program using the same job/file interface. Vendor SDKs, GUI applications, MPI and cloud services retain their own launch/session requirements. Exchange units, coordinates, sampling, normalization and material sources alongside fields, spectra, S-parameters and models.

## Development

```sh
python -m pip install -e . build mcp -r requirements-science.txt
python scripts/build.py
OPTICS_SCIENCE_TESTS=1 python -m unittest discover -s tests -v
python scripts/check_standalone_connection.py
```

PowerShell: set `$env:OPTICS_SCIENCE_TESTS="1"` before running the tests. Native Windows/macOS/Linux jobs run scientific calculations, background scheduling, cancellation, installed-wheel/plugin checks and real MCP disconnection/reconnection.

Host format sources: [Codex](https://developers.openai.com/plugins/build/plugins), [Claude Code](https://code.claude.com/docs/en/plugins-reference), [DeepSeek Harness](https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory).

[Local commercial software: discovery, sessions, projects and results](NATIVE.md).

## Add a research tool outside the catalogue

Register its local description, then bind its installed environment, or give it a recipe (`pip`, `conda`, `python`, `platforms`) so that `prepare_environment` and submissions install it. The published catalogue remains unchanged.

```python
import sys
from optics_photonics import environments

environments.register('lab-wave-model', 'Lab wave model',
    'Our numerical wave-propagation model', modules=['numpy'], distributions=['numpy'])
environments.bind('lab-wave-model', python=sys.executable, threads=2, concurrency=4)
```

Use `submit_calculation` or `submit_workflow` with this tool ID and a complete native program. For a C/C++/Java/Julia or other executable, register with `kind='command'`, then bind an executable/argument array. It receives `OPTICS_REQUEST_FILE`, `OPTICS_OUTPUT_DIR` and `OPTICS_RESULT_FILE`, exactly like other command adapters. A custom Python environment can also run `sample_episodes`.

MCP exposes `register_software` and `registered_software`; `software_details` reads both published and locally registered software. Shell clients use `optics-photonics register-software @definition.json` and `registered-software`. Registration describes software; binding selects an installed interpreter or executable. Installation remains under the tool's own requirements and license. Keep files under the job's OUTPUT_DIR so parallel computations own separate outputs.

`optics-photonics connect portable --output DIR` creates an Agent Plugins package with root plugin.json, mcp.json and skills/.
Portable packages use a bare `uv` command and plugin-relative paths, so moving or updating a package does not retain paths from its author’s computer. `connect codex --managed-python` and `connect claude --managed-python` generate similarly movable native packages. Local packages generated without that option retain the selected Python.
The `read_guide` tool and the MCP resources `optics://guides/TOPIC` return both Skills and each of their references on demand. A host that shows MCP prompts lists six starting points, each a complete instruction for one kind of work: `calculate`, `study`, `reproduce`, `connect`, `measure` and `continue`. Tool annotations state which tools only read and which run programs or replace settings.
