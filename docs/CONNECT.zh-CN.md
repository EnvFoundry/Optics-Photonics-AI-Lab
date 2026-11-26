# 光学与光子学研究工具

Optics & Photonics 独立提供光学软件连接、原生研究程序、后台计算、MCP、Skills 和插件。选择软件、建立模型、求解、优化、保存与读取结果，都通过这个软件包完成。

## 安装与第一次计算

连接程序使用 Python 3.10+，支持 Windows、macOS、Linux。科学软件使用各自支持的 Python、系统与许可；可以为每个软件连接不同的环境。

```sh
python -m pip install "https://github.com/EnvFoundry/Optics-Photonics-AI-Lab/releases/download/optics-v0.11.3/optics_photonics-0.11.3-py3-none-any.whl"
optics-photonics search "thin film" --license-group open
optics-photonics info tmm
optics-photonics prepare tmm
```

将输入保存为 `inputs.json`：

```json
{"n":[1,1.45,1.52],"thickness_nm":[100],"wavelength_nm":[450,550,650]}
```

```sh
optics-photonics run tmm spectrum --inputs "@inputs.json"
```

命令返回输出目录、结果和软件版本。光谱保存为带单位和坐标轴的 `spectrum.npz`，同时有 CSV。`info tmm` 列出该软件的封装程序及每个输入的默认值和含义，`info tmm --program spectrum` 打印程序源码，修改后可用 `--program` 提交。没有给出的输入保持默认值，不认识的输入名会被拒绝。也可先下载 Release 中的 wheel，再用 `python -m pip install 文件.whl` 安装；也可用 `python -m optics_photonics` 调用全部命令。

## 科学环境与完整程序

`prepare` 按已有配方建立独立环境；已有环境直接连接：

```sh
optics-photonics bind tmm --python "/path/to/scientific/python" --threads 1 --concurrency 4
optics-photonics environments
optics-photonics resources --cpu 8
```

`threads` 设置常见数值库的线程数；`concurrency` 设置同一软件的并行进程数；`resources` 设置这台计算机上所有后台研究任务共享的 CPU 额度。MPI、GPU、商业许可证和集群队列使用求解器自己的启动配置。`bind --python PATH` 不写软件 ID 时，连接这个解释器里能导入的所有目录软件和本地登记软件，已经连接的软件保持原环境。

### 安装配方

[connections.json](../data/connections.json) 中有 50 项开源软件带安装配方；`software_search` 的 `install` 字段给出方式：`pip`、`conda` 或 `manual`。

- **pip**：固定版本的依赖；只在 GitHub 发布的软件（laserfun、PtyPy、py-fmas）固定到某次提交。PATH 中有 [uv](https://docs.astral.sh/uv/) 时由 uv 建立环境，并提供配方要求的 Python 版本（PoPPy、GDSFactory 需要 3.12，Strawberry Fields 需要低于 3.12）；没有 uv 时使用连接程序自身的 Python 和 pip。
- **conda**：没有可用 wheel 的软件从 conda-forge 安装：Meep 与 MPB（共用一个环境，没有 Windows 版本）、FEniCSx、Gmsh、HoloPy。安装程序依次取 `OPTICS_CONDA`、PATH 中的 micromamba、mamba、conda；都没有时下载本平台的官方 micromamba 到 `OPTICS_HOME/bin`，并核对其公布的 SHA-256。
- **platforms**：wheel 不覆盖所有平台时，配方列出可安装的 conda 平台名，例如 Raysect 只有 `linux-64`；在其他平台上报错并说明。
- **manual**：商业软件和需要编译器或厂商安装程序的软件（Lumerical、COMSOL、OpticStudio、CST、JCMsuite、openEMS、PyNX、SMUTHI）按各自说明安装后再连接；`installation_note` 记录需要注意的地方，例如 PyPI 上同名但无关的 `meep`、`pynx`。

提交计算时如果软件还没有环境，会先安装：同一次运行多一个 `prepare.TOOL` 步骤，成功后计算开始。环境只建立一次，再次准备只补装缺少的部分；两个运行同时准备同一个环境时依次进行。`prepare TOOL --package scipy`（MCP 中为 `packages`）向已准备的环境添加依赖，并保留其他设置。新准备的软件每个进程一个线程，进程数只受共享 CPU 额度限制；`bind_environment` 可以修改这两项。安装的版本（包括 conda-forge 包的版本）记录在环境连接中。[scripts/check_recipes.py](../scripts/check_recipes.py) 在临时目录中为本平台准备全部配方并逐项报告；加 `--programs` 时接着用默认输入运行每个软件的全部封装程序，并报告对照结果。

原生 Python 程序接收 `INPUTS`、`OUTPUT_DIR`，并将简洁结果赋给 `RESULT`。程序可以使用完整 API，保留同一软件会话，运行优化、迭代和参数扫描。数组、场、网格和模型写入 `OUTPUT_DIR`。

```sh
optics-photonics run tmm --program research.py --inputs "@inputs.json"
```

原生命令也可以连接。程序从 `OPTICS_REQUEST_FILE` 或标准输入读取 JSON，将文件写入 `OPTICS_OUTPUT_DIR`，把 JSON 结果写入 `OPTICS_RESULT_FILE`。`bind --command-json` 接收可执行文件和参数数组，支持 `{request}`、`{output}`、`{result}` 参数。C、C++、C#、Java、Julia 和商业软件批处理均可沿用其原生实现。

## 持续计算与研究工作流

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

提交后立即返回任务编号。后台进程继续执行，CLI 或 MCP 关闭不会结束计算。再次提交同一请求时沿用 `request-id`，会返回原任务。运行期间已经写出的文件可以直接读取。`files --prefix STEP/` 只列某一步的文件，`array` 读取 NPY 或 NPZ 数组的一部分，不需要载入整个文件；加 `--image 文件.png` 时把这部分画成曲线或带坐标轴和色标的二维图（复数场用 `--part phase`，跨数量级用 `--scale log`）。

工作流是 JSON 数组。每一步包含 `id`、`tool`、`inputs`，以及 `action` 或 `source`。`after` 指定前置步骤；`bindings` 把前一步结果写入下一步输入，并自动建立依赖。数组、网格、模型这类大数据按文件传递：`{"field":{"step":"solve","file":"field.npz"}}` 把输入 `field` 设为该步骤写出的文件或文件夹在计算机器上的位置；引用相对于本次运行保存，步骤开始时才解析成路径。例如：

```json
[
  {"id":"a","tool":"tmm","action":"spectrum","inputs":{"n":[1,1.45,1.52],"thickness_nm":[100],"wavelength_nm":[550]}},
  {"id":"b","tool":"tmm","action":"spectrum","inputs":{"n":[1,1.45,1.52],"thickness_nm":[120],"wavelength_nm":[550]}},
  {"id":"compare","tool":"tmm","source":"RESULT={'delta_R':INPUTS['b']-INPUTS['a']}","bindings":{"a":{"step":"a","path":["result","first",1]},"b":{"step":"b","path":["result","first",1]}}}
]
```

```sh
optics-photonics submit-workflow workflow.json --request-id comparison-001
optics-photonics submit-workflow next-steps.json --run-id RUN_ID
optics-photonics cancel RUN_ID
optics-photonics resume RUN_ID
```

没有依赖的步骤按资源设置并行；后续步骤等待依赖完成。追加步骤保留同一个研究任务。`resume` 保留已完成步骤，重新运行未完成步骤，每次尝试的文件独立保存。云端求解器的任务编号和接续方式由原生程序保存与处理。

### 参数扫描与优化

```sh
optics-photonics submit tmm spectrum --inputs '{"n":[1,1.38,1.52],"wavelength_nm":[550]}' --sweep '{"thickness_nm.0":{"start":50,"stop":150,"points":21},"n.1":[1.30,1.38,1.46]}'
optics-photonics sweep RUN_ID --value first.1 --minimize first.1 --save
optics-photonics submit tmm spectrum --run-id RUN_ID --inputs '{"n":[1,1.30,1.52],"wavelength_nm":[550]}' --sweep '{"thickness_nm.0":{"start":100,"stop":110,"points":21}}'
optics-photonics run tmm spectrum --inputs "@coating.json" --optimize '{"variables":{"thickness_nm.0":[0,300],"thickness_nm.1":[0,300]},"minimize":"mean.R"}'
```

扫描的参数点并行计算，程序很快时一个进程依次计算多个点；`thickness_nm.0` 这样带点的名称只设置列表输入中的一个元素。`sweep` 列出每个点的扫描输入和选定的结果值，给出最优点；加 `--save` 写出按扫描输入排成网格的 `sweep.npz`。`--run-id` 把更细的扫描追加到同一次运行。`--optimize` 在一个进程内于给定范围搜索变量，方法为差分进化加 Nelder–Mead 精修，或单独使用 Nelder–Mead。完成后，最优一次计算的文件写入 `best/`；运行或中断期间，进度记录指向当前最优文件（[参数研究](../skills/optical-computation/references/studies.md)）。

## 从插件市场安装

| 宿主 | 添加来源与安装 |
| --- | --- |
| Agentic Research OS | **Extensions → Plugins → Add from link**，填写 `https://github.com/EnvFoundry/Optics-Photonics-AI-Lab/tree/main/connect/agentic-research-os`，安装 `optics-photonics`，激活返回的修订。 |
| Codex | `codex plugin marketplace add Cherish0925/Optics-Photonics-AI-Lab`，再执行 `codex plugin add optics-photonics@optics-photonics`。 |
| Claude Code | `claude plugin marketplace add Cherish0925/Optics-Photonics-AI-Lab`，再执行 `claude plugin install optics-photonics@optics-photonics`。 |

Research OS 包携带连接器、目录与 Skills，使用宿主自带的 Python。
Codex/Claude 的 GitHub 插件及 DeepSeek 包通过 [uv](https://docs.astral.sh/uv/getting-started/installation/) 选择 Python 3.10+，缺少时下载。执行端安装一次 uv 即可；已有 Python 时，也可以直接用它生成下面的宿主包。
Codex 使用插件相对工作目录，Claude 使用自己的插件路径变量。
`connect portable`、`connect codex --managed-python` 和 `connect claude --managed-python` 生成可跨机器移动的包；不带该选项的本机包保留选定的 Python 路径。
科学软件通过 `prepare_environment` 准备，或通过 `bind_environment` / `bind_native_software` 接入；提交计算时如果该软件还没有环境，会先按安装配方自动准备。

更新时在 Research OS 刷新来源，安装其新修订。Codex 使用
`codex plugin marketplace upgrade optics-photonics` 刷新来源并安装更新；Claude 使用
`claude plugin marketplace update optics-photonics`，再执行
`claude plugin update optics-photonics@optics-photonics`。
新连接加载新版插件；科学环境与研究记录保留在 `OPTICS_HOME`，已提交的计算继续使用启动时的程序。

这些入口是项目维护的 GitHub 插件市场；OpenAI、Anthropic 审核后的公共目录另有提交与收录流程。

## Agent、MCP 与插件

```sh
optics-photonics connect agentic-research-os --output optics-plugin
```

| 系统 | 接入形式 |
| --- | --- |
| Agentic Research OS | 安装含 `plugin.json` 的目录，激活对应修订；Release ZIP 解压后直接得到这个目录。 |
| Codex | `connect codex` 生成原生插件和 MCP 注册命令。 |
| Claude Code | `connect claude`；使用 `claude --plugin-dir PATH` 或生成的 MCP 命令。 |
| DeepSeek Harness | `connect deepseek`；使用 `dsh web --patch PATH/optics.cordis.yml`。 |
| Qiushi | 研究系统版本使用其原生插件包；其他版本使用 `connect qiushi` 生成的 MCP 或命令入口。 |
| 其他 MCP 宿主 | `connect mcp`，按宿主格式注册 stdio 命令。 |

MCP 包含软件检索、材料折射率（`materials`，来自 refractiveindex.info 数据库）、环境准备、连接、资源设置、提交、任务查询、取消、继续、文件读取与下载。`prepare_environment` 在后台按配方安装，返回任务编号；安装完成后软件环境自动登记。提交计算时如果软件还没有环境，会自动先安装。操作方法和研究方法由两个 Skills 及其参考文件按需提供；不加载 Skills 的宿主用 `read_guide` 读取其中任何一份。

`environment_prepare`、`prepare_environment`、`submit_calculation`、`submit_workflow`、`sample_episodes` 和 `resume_run` 支持可选的 MCP Tasks。传入 `task: {}` 时，原有 run ID 就是 Task ID；`tasks/get`、`tasks/result` 和 `tasks/cancel` 查询或控制同一份工作。环境准备保留 ready/pending 结果，计算任务返回最终运行记录及结果。普通调用保持原来的返回方式，`run_status` 仍是有界的即时查询。计算尚在运行时须保留其执行容器；脱离父进程的 worker 不能跨容器终止存活。

计算发生在 MCP 服务所在的计算机。远程机器可通过 SSH 启动 stdio 服务，保持连接程序与科学环境在同一台机器：`ssh -T USER@HOST /path/to/python -m optics_photonics mcp`。`read_result` 把内容传回 Agent，`read_array` 传回 NPY/NPZ 数组的一部分；`download_result` 写入服务所在机器的目标路径。TypeScript 客户端的 `download` 通过连接把文件内容传到客户端所在的机器；特别大的远端文件仍可用 SSH/SFTP 取回。

更新插件时沿用持久目录。连接配置与研究记录使用 `OPTICS_HOME`，默认 `~/.optics-photonics`。软件使用 `OPTICS_SOFTWARE_HOME` 指定的目录，否则使用 `OPTICS_HOME/environments`。Research OS 将新软件放在 `System/software/optics-photonics`；已有安装原位复用。知识与工作流通过包内资源入口读取。

## 软件覆盖与扩展

软件目录包含商业、开源和受限许可三组，每项保留原始接口文档及已有 MCP、Skill 的来源。交互指南可以筛选带封装程序的软件。`software_details` 返回原生 API、安装配方，以及每个封装程序的物理模型、输入和对照结果。

新增软件时补充连接记录、环境说明和原生程序，沿用共同的任务与文件接口。涉及商业 SDK、GUI、远程计算或 MPI 时，把对应启动与会话管理放在软件适配程序中。跨软件传递场、光谱、S 参数和模型时，同时保存单位、坐标、采样、归一化与材料来源。

[本机商业软件：发现、会话、项目与结果](NATIVE.zh-CN.md)。

长时间研究任务应运行在持续存在的计算主机上。独立工作进程可以在提交程序退出后继续计算；如果宿主回收整个容器或进程组，工作进程也会结束。此类 Agent 环境应通过 SSH 连接科学工作站上的 MCP，将计算留在工作站。

## 接入目录之外的研究工具

通过 `register_software` 登记本地软件的名称、用途、Python 模块与资料来源，再用 `bind_environment` 连接已经安装的解释器或可执行程序；也可以给出安装配方（`pip`、`conda`、`python`、`platforms`），由 `prepare_environment` 和提交计算自动安装。`registered_software` 列出本地工具，`software_details` 读取其说明。目录中的已有软件 ID 保持原有含义。

Python 示例：

```python
import sys
from optics_photonics import environments
environments.register('lab-wave-model', '实验室波动模型',
    '研究组开发的波动传播程序', modules=['numpy'], distributions=['numpy'])
environments.bind('lab-wave-model', python=sys.executable, threads=2, concurrency=4)
```

随后用该 ID 提交完整研究程序、并行工作流或 episode 采样。C/C++、Java、Julia 等可执行程序使用 `kind='command'` 登记并绑定命令数组，沿用请求文件、输出目录和 JSON 结果接口。程序应将输出写入该任务的 OUTPUT_DIR，避免并行计算共享临时文件。登记不代替软件本身的安装与许可。

`optics-photonics connect portable --output DIR` creates an Agent Plugins package with root plugin.json, mcp.json and skills/.
`read_guide` 工具和 MCP 资源 `optics://guides/TOPIC` 按需返回两个 Skills 及其每一份参考文件。支持 MCP 提示的宿主会列出六个起点，每个都是一类工作的完整指令：`calculate`、`study`、`reproduce`、`connect`、`measure`、`continue`。工具标注说明哪些工具只读取，哪些会运行程序或替换设置。
