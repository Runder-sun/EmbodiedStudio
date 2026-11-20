# 自主生成标准交付物

仅在当前生成任务声明 `deliveryContract='producer-exports-v1'` 时使用。先从 `project_inspect` 读取当前需求、身份、冻结版本、回合和实际工具位置。旧任务不追补本契约；需求和质量仍由你及内部审核者负责。

## 按请求交付

### 用户显式只保留原始录像

用户新指令及 `project_inspect.recordOnlyVideos=true` 优先于历史 producer-exports-v1、内审和正式交付要求。此模式只用于 `videoOnly=true`、`requiredOutputs=["demonstrations"]`，由 `recordOnlyTaskIds` 锁定本次真实任务；每个任务仅需一条真实机器人从原始初态完整完成原任务的成功录像，已有真实成功记录直接复用。完成后仅报告原始视频引用及 taskId、revision、episodeId，不复制、改编码、打包或制作产物清单，不输出训练数据，不召集 `scene_quality_review`，不执行独立审核、消费者或 `project_deliver` 交付检查。保留场景、物理和任务完成条件，保留真实源、执行、失败与中断；记录成功不冒称独立审核或格式检查通过。以下导出和审核规则在此用户显式模式中不适用；仅有 `videoOnly=true` 时仍按原流程。

### 仅全过程视频

`project_inspect.videoOnly=true` 时，demonstrations 只交用户要求的完整机器人执行视频，不生成 LeRobot、HDF5 或其他训练数据；其余未请求产物仍省略。真实机器人控制、完整原任务成功证据和内部独立审核照常保留，不把测试夹具、拼接片段或正确终态当成功视频。
没有 `environmentRefs` 的新录像任务可以在本工程冻结所需环境；已有显式环境引用仍保持只读，禁止重建。环境和生成原始记录照常留存，但不要求额外导出环境产品或训练轨迹。
新录像没有初始环境引用时，内审和产物清单按交付接口实际推导的 `environmentRefs=[{revision: 当前交付版本}]` 绑定，并带 `requiredOutputs=["demonstrations"]`；不能把未定义的初始引用写入最终清单。

产物清单与内部审核的原有 binding 顶层另加 `videoOnly:true`。`products.robot` 为非空列表，每项仅含 `{video:"T02.mp4",mapping:"T02-video.json"}` 两个相对文件路径。每个 mapping 的 `episodes` 只含一条，保留与本次选择完全相同的 `{episodeId,taskId,revision?,use}`，另加 `sourceVideo:"episodes/<episodeId>/<实际原始视频相对路径>"`。此路径相对本工程远端项目目录；直接复制整条原始视频，接收器核原件字节、完整解码和执行来源，不接受裁剪、重编码、替换或跨工程文件冒充该执行。视频取景和全过程成功仍由内部审核者自主确认。

按原流程在 `delivery/` 导出，清单 `files` 列视频与 mapping 的真实字节及摘要，绑定固定执行集合，再申请 `project_deliver`。视频文件名无任务种类要求；格式修复继续复用原回合，无须重新仿真。`videoOnly` 缺省时保持原双训练格式合同。

读取本次 `requiredOutputs/environmentRefs`，只导出相应产品：generation对应rawTrace、ATIF和训练JSONL；videoOnly缺省时demonstrations对应同源LeRobot与HDF5；evaluation对应冻结环境、任务与评分包。未请求项在products中省略，不补假目录或自报通过。机器人阶段仍保留真实生成过程，但没有要求第一产物时不必重新导出它。

新范围合同的清单 `binding` 同时包含与宿主完全相同的 `requiredOutputs/environmentRefs`。`episodes` 逐项带真实 `{episodeId,taskId,revision,use}`；每个机器人组 `mapping.episodes` 也带同一个revision，不能用最新环境版本覆盖所有回合。同一物理回合的两个格式、多相机及重渲染只算一条原始轨迹。环境包可以在相同data root闭包中保留同工程多个选定revision，必须包括清单内每个所选版本的manifest/资源/runtime和运行入口；不能只打包最新一版。

独立采集的behaviorRef与代码闭包也属于原始来源，随机器人数据说明保留；存在机器人安装时，同时保留每回合的robotSetupRef/robotSetupSha256、配置与模型依赖闭包、组合I/O及录制声明。独立评测使用同一冻结安装引用，打包保留data root内的相对关系，不依赖作者原工程的绝对路径。场景包可以不含机器人或专家入口。当前整个workspace的sourceHash只绑定本次导出与审核期间不变，环境身份和独立行为身份分别引用冻结闭包，改行为或格式不要求重建旧环境。

## 分工与工作区

系统保留真实公开事件、源码快照、冻结场景、逐周期物理事实和来源，提供官方格式工具及既有交付接口。你在本工程 `delivery/` 与远端 `projects/<projectId>/delivery/<run>/` 内编写并运行导出、校验、启动和打包脚本。这些是可修复的项目产物，不是系统内核。原始记录和工具环境只读。

先对已选输入与用途建立一套映射，再由同一映射输出两种机器人格式；使用标准读取器回读后，由内部审核者核对数据语义、权限和原需求。导出脚本、字段映射、路径或格式错误由你自主修正、重新导出。复用同一真实回合，格式修复无需重新仿真。只有可信采集、官方工具环境或系统发布接口本身的实际故障，才使用 `generation_wait_system`。

## 三种交付物

| 产物 | 输出与用途 |
| --- | --- |
| LLM 轨迹 | 原始公开事件及引用、`ATIF-v1.7`、训练 JSONL；恢复真实工具与代码决策，保留失败和缺口 |
| 机器人轨迹 | 同源 LeRobot v3 和 robomimic `SequenceDataset` 可读 HDF5；原始回合仍保留为事实基准 |
| 仿真评测环境 | 同版场景/资源/机器人、任务与 rubrics/案例、观测动作合同、可获得运行依赖、原样评测入口与使用说明 |

格式可读不证明场景质量、任务成功或学习效果。本次交付范围要求完成机器人任务或成功示范时，仍需真实成功执行与内部审核，失败和截断不改成成功示范。

### LLM 轨迹

从系统原始公开轨迹的一个完整事件前缀派生，清单锁定前缀字节数和 SHA256。原件不按训练长度删除。每条训练记录用非空 `sourceEventIds` 指向该前缀内的真实事件，并包含 `messages` 或 `prompt`/`completion`。

`project_inspect.recording` 提供现有工作台轨迹文件、仿真主机数据根下的镜像相对路径、`.trace/sources` 与 `.trace/inputs` 目录及最后镜像状态。它只增加发现信息；`project_inspect` 仍会执行原有源码快照和调用记录。原档案保持只读，由你在 `delivery/` 自主复制与打包，不修改系统原件，也不依靠当前源文件或工具打印片段拼出历史档案。

原始引用也属于第一产物。扫描已锁定 raw 前缀中的结构化 `snapshotRef/inputRef/beforeSourceRef/afterSourceRef`，包含其他生成轮次的引用；`source_snapshot.previousSourceHash` 对应的历史源也须保留。读取输入档案后继续处理 `instructionsRef` 等同类引用。仅处理真实路径字段，不从代码、命令、正文或 diff 猜引用。将引用的真实原始 JSON 字节复制到 `products.generation.archiveRoot` 下，保留 `.trace/sources/<hash>.json` 和 `.trace/inputs/<hash>.json` 的相对布局，逐项列入 `files`。同工程 `projects/<projectId>/.trace/...` 引用解析到相同布局；不得跨工程、使用软链接或添加猜测别名。

两类摘要不同：sources 文件名及 `sourceHash` 是 `sourceFiles` 映射的紧凑 JSON 摘要（保留键顺序与中文，不能重排或修剪源码空白），不是档案 JSON 文件的字节摘要；inputs 文件名及 `inputHash` 是档案原始字节 SHA256。清单 `files[].sha256/bytes` 另外锁定每份档案的真实文件字节，包括 JSON 空白。冻结时消费者会与原工程原件逐字节比对，冻结后仅靠包内原档案复验；重新序列化后内容相似不能代替原件。

`archiveRoot` 只在这个 raw 前缀实际引用源或输入档案时必需；前缀没有此类引用时可省略，不制造空档案。消费者的 `archiveClosure.complete` 仅表示所锁 raw 前缀的源、输入及传递指令档案闭包完整，不代表隐藏上下文、完整原生 session 或所有媒体/环境引用均被归档。截止后追加的事件与档案属于后续导出，不污染已冻结前缀；截止前已经引用的档案不能以截止为由省略。未请求 generation 的机器人数据交付无需补第一产物；旧 binding 省略 `requiredOutputs` 时仍默认完整三产物。

- 工具调用与返回按真实 call ID 配对，训练消息保留真实 `tool_call_id`，其他训练模板在其映射中保留等价调用关联；合并同一原生条目的流式增量，区分当时截断返回与后台完整报告。原生条目已记录调用名称、参数与结果但没有独立模型 call_id 时，可用该条目稳定身份建立并说明关联，不能因此丢掉真实工具动作，也不声称恢复了未知原模型字段。公开命令、文件编辑及其后果不能在 ATIF/训练投影中静默漏掉；未识别的公共编辑保留原件和缺口。只有 diff 或快照、没有原始调用时，只能记录已发生的变更，不能反推助手调用或意图。
- 主、子会话各自保留有来源的输入和历史，只按真实委派关系链接。子代理没有原始委派或消息输入时标明未知，不能用根任务替代并当作上下文完整的 SFT；也不能把并发消息拼成一个代理的历史。
- 公开助手消息和有来源的实际工具调用动作可作相应助手目标，工具返回是观察；活动通知、等待状态及其他工具/系统观察不合成为助手发言。输入只含动作前已知事实，修改后的代码和后续结果不得倒灌输入。私有推理和助手 analysis 不进入公开 ATIF/训练视图；模型、完整上下文、token/logprob 与结束时间缺证据时保持未知。
- 失败留在原件中，训练保留失败及其后果、后续修正和验证的关系。按用途区分目标、观察与采样权重，不默认每次等待或失败都是同权正例；真实的排错红例、有效但未成功的尝试也不一概删除。
- 按完整决策或失败—修复窗口和实际目标模型的 tokenizer 预算切分，记录覆盖与截断；不用固定步数代替预算，也不靠字符串截断破坏 JSON、拆散调用/返回或留下孤立 tool 消息。相同工程、分支和修复片段保持同一数据划分，助手目标与历史助手/用户/工具观察的训练遮罩分开。尚未选定模型时可先交可复用 messages，明确未做该 tokenizer 的预算与 token 级遮罩。完整调用—返回单元仍超预算时保留原件并说明未覆盖部分，不强塞或补造缺失调用。
- 内审从 raw 实际追查有价值的决策或失败—修复链，核对其编辑、工具返回、后果及适用子分工在 ATIF/训练中的来源和覆盖，必要缺口照实说明。JSON 可读或官方读取器通过不能代替这项语义核对；不为凑链补造调用、子任务输入或成功结果。

Harbor 出题中的八字段是可选方法摘要，不能代替完整交互，也不带入固定时长、双模型、轮数或系统自定分数。

官方模型与读取器见 [Harbor ATIF](https://docs.harborframework.com/agents/atif)。显式写 `schema_version='ATIF-v1.7'`，不要跟随库的默认版本。步骤从 1 连续，`observation.results[].source_call_id` 对应本步骤真实 `tool_call_id`。使用官方 `Trajectory.model_validate()` 与 `TrajectoryValidator.validate(Path(...))`，同时检查本地图片引用；自定义来源与缺口放 `extra`。

### 机器人双格式

先读取冻结 I/O、Task、episode 和媒体帧表。`examples/read_demonstrations.py` 的完整资格读取需要匹配的 SDK 环境；CPU 格式脚本读取已经核验的同一原始文件及其摘要，不能在 CPU 上绕过 SDK 检查后声称重新验证了物理。

映射至少说明源 episode/用途、特征来源、端口与关节顺序、shape/dtype、单位/坐标、命令语义、控制时钟、相机权限、源帧时间和标注来源。不同维度或不同语义分组，不按品牌写白名单，也不默认补零拼接。

- `executedAction` 是实际下发并读回的驱动目标；实际 q/dq 等状态另存。持久端口省略时从本回合 `nativeInitialTargets` 和此前命令保持；旧记录缺初值就保留未知或报告此 profile 缺项，不能用构建探测值或 0 补齐。瞬时 effort 不暗补。
- 每行对应真实控制周期 `observation_t → command_t → observation_next`。高频物理样本另存，不能冒充额外策略步。低频图像若按最新已采帧保持，保留源帧 ID、实际采样时间及 age，两格式用相同源帧映射；不取未来帧或造黑图。
- 原策略允许的 stream/`policyCameraIds` 与额外观察相机、评分真值分开。采用额外视角训练新策略时显式声明新的训练观察合同，不声称采集策略看过它们。保留每帧内外参及模拟传感器/外部观察来源。
- 任务文字与生成器阶段标注可随数据提供；rubric 标注保持 `generated_interpretation`、周期与来源，不冒充人工真值或传感器事件。深度和像素语义未实际采集就写未支持。
- `terminated`、`truncated`、`success` 分开，未知 reward 不补 0。仅行为克隆不需要的未知监督可明确省略；需要它的训练用途保持缺项。自然完整回合保存；训练窗口不跨回合，边界 padding 带 mask，统计只用训练划分。

LeRobot 使用锁定官方 `LeRobotDataset.create(...)`、`add_frame(...)`、`save_episode()`、`finalize()`；`finalize()` 后再新建本地 reader 并实际读数值和视频，不能只看目录存在。特征形状由本次实际机器人决定。每帧包含真实特征和 `task`；`timestamp/frame_index` 由 writer 按 fps 生成，实际采集时间另存源时钟特征或映射，不把容器时间冒称采样时间。参考 [官方 v3 说明](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)。

下面是库调用骨架，`features`、`fps` 和帧均由你从同一真实数据映射产生，不是系统预定机器人向量：

```python
from lerobot.configs import RGBEncoderConfig

writer = LeRobotDataset.create(
    repo_id, root=lerobot_root, fps=fps, features=features,
    robot_type=actual_robot_type, use_videos=True, video_backend='pyav',
    encoder_threads=1, rgb_encoder=RGBEncoderConfig(vcodec='h264'),
)
try:
    for episode in canonical_episodes:
        for frame in episode:
            writer.add_frame(frame.copy())  # 包含实际 features 和 task
        writer.save_episode(parallel_encoding=False)
finally:
    writer.finalize()
loaded = LeRobotDataset(repo_id, root=lerobot_root,
                       download_videos=False, video_backend='pyav')
sample = loaded[0]  # 实际读数值与像素，再与源映射核对
```

编码参数按数据取用需求选择；上例单线程是小型调用示例，不是所有场景的性能上限。

实际读取后核对 dtype 和 shape：默认转换可能降低浮点秒钟精度，单元素特征也可能返回零维张量。源时钟优先保留原始整数 tick、timeOrigin 和速率；派生整数时间要说明精度。只有读取器压缩了单元素形状时，按已声明的实际 feature shape 恢复，不补造数值。

HDF5 使用 `h5py` 写 `data/demo_i/obs/<key>`、`next_obs/<key>`、`actions` 和 `num_samples`，图像为实际 uint8 像素，不能只放视频路径。用官方锁版 `robomimic.utils.dataset.SequenceDataset` 读取；先通过 `ObsUtils.initialize_obs_modality_mapping_from_dict()` 声明 low_dim/RGB，再传实际 `obs_keys`、`action_keys`、`action_config`。`lang=None` 不加载额外语言模型；任务文字和来源可放扩展元数据。只承诺其离线训练布局，不能伪造 MuJoCo `states/model_file` 或环境类型。参考 [官方 Dataset API](https://robomimic.github.io/docs/modules/datasets.html)。

```python
import robomimic.utils.obs_utils as ObsUtils

ObsUtils.initialize_obs_utils_with_obs_specs({'obs': modality_keys})
reader = SequenceDataset(
    hdf5_path=str(hdf5_path), obs_keys=obs_keys,
    action_keys=['actions'], dataset_keys=[],
    action_config={'actions': {'normalization': None}},
    seq_length=window_length, get_pad_mask=True,
    hdf5_cache_mode='low_dim', load_next_obs=True, lang=None,
)
try:
    sample = reader[0]
finally:
    reader.close_and_delete_hdf5_handle()
```

两种读取结果核对同一 episode、动作、状态、源时间、源帧、用途与标注。有损 MP4 解码与原 PNG 的像素差异如实注明，不要求压缩后文件摘要相同。不要分别重算映射，也不要把两份文件称作两次独立采集。

### 仿真评测目录

环境根保持现有相对引用结构，清单只复制必要公开文件：

```text
<environment.root>/
  projects/<projectId>/revisions/<revision>/manifest.json
  projects/<projectId>/revisions/<revision>/...同版源、stage、资源闭包
  projects/<projectId>/episodes/<selectedEpisode>/...必要记录与媒体
  runtime/<runtimeCodeHash>/runtime/...冻结内核
  jobs/<publicationJob>/{status,result}.json
```

必要的 publication 终态文件维持已签发布来源。不要直接归档整个工程或 jobs，不带 PID/进程控制文件、评分私态、隔离探针、认证、临时缓存和其他工程。受许可限制不能随包再分发的资源提供准确获取和安装说明，不从 BEHAVIOR 加密数据中抽出资产。

原样复制本系统 `examples/evaluate.py` 作为 `entrypoint`，README 给出 `--root <environment.root>`、manifest、兼容候选与显式 GPU 的运行方式。保留冻结 worker、实际依赖与解释器/ABI 校验；当前只声明实际可获得的 Linux/NVIDIA 运行环境。把依赖安装或镜像/前缀取得方式、版本和许可写清楚，不能删校验或仅改绝对路径冒称可移植。

protocol 说明任务指令、实体绑定、允许观察动作、初态/seed、时长、终止/截断、rubrics、案例及结果含义。当前固定 seed 就声明固定实例，不捏造随机化或泛化划分。正式消费者对该新根执行既有加载和最多一周期接口检查；不是整条任务的第二次稳定成功门。

## 官方 CPU 工具

实际数据根从宿主工程指令取，当前部署使用其下 `envs/delivery-readers-v1/bin/python`；固定官方来源在 `tools/delivery-readers-v1/`。`project_inspect.runtimeExecution` 是仿真 SDK，不把它当 CPU 格式解释器。依赖与来源由 `environment.json` 留存，和 Isaac SDK 环境分开，不修改共享工具或 SDK。

LeRobot/h5py 是正式安装库；Harbor 官方 wheel 与 robomimic 官方锁版源码只作有限模型/读取器。它们没有安装完整 Harbor 调度器或 robomimic 仿真/训练算法依赖。可在自己的导出脚本中按实际数据根添加这两条固定工具路径，再使用官方 API：

```python
import sys
from pathlib import Path

tools = Path(data_root) / 'tools/delivery-readers-v1'
sys.path.insert(0, str(tools / 'harbor-0.24.0-py3-none-any.whl'))
sys.path.insert(0, str(tools / 'robomimic-d309eaecc18acf4152a830a895a6984b8ac71b05'))
from harbor.models.trajectories import Trajectory
from harbor.utils.trajectory_validator import TrajectoryValidator
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from robomimic.utils.dataset import SequenceDataset
import h5py
```

本地 reader 用 `root=`、明确视频后端和离线文件，不调用 Hub 上传。工具错误先读该锁版 API 与真实报错，修本工程脚本；工具来源损坏或固定依赖本身不可导入才报告系统缺陷。

## 交付清单

清单路径为远端 `projects/<projectId>/delivery/<run>/manifest.json`。所有产物路径相对该清单目录；`files` 逐项列实际普通文件的 `path/bytes/sha256`，目录由其中完整文件组成。清单不把自己列进 `files`；`project_deliver` 用独立摘要固定它，避免自引用。

```json
{
  "schema": "sceneagent-produced-artifacts-v1",
  "binding": {
    "projectId": "当前工程", "generationId": "当前生成任务",
    "requirementVersion": 1, "requestHash": "真实需求摘要",
    "sourceHash": "当前工程源审核绑定摘要", "revision": "冻结版本",
    "requiredOutputs": ["generation", "evaluation"],
    "environmentRefs": [{"revision": "冻结版本"}]
  },
  "episodes": [],
  "trace": {"bytes": 123, "sha256": "真实公开轨迹前缀摘要"},
  "files": [{"path": "实际相对文件", "bytes": 123, "sha256": "实际文件摘要"}],
  "products": {
    "generation": {"rawTrace": "generation/raw.jsonl", "archiveRoot": "generation/originals", "atif": "generation/trajectory.json", "training": "generation/train.jsonl"},
    "environment": {"root": "environment/data", "entrypoint": "environment/evaluate.py", "readme": "environment/README.md", "runtime": "environment/runtime.json"}
  }
}
```

这是仅交 generation 与 evaluation 的场景范围示例，不要求机器人示范。数字、身份和摘要均从实际输入计算；示意值不能提交。请求 demonstrations 时，按本次真实范围替换 `binding.requiredOutputs/environmentRefs`，加入实际 `products.robot` 双格式入口，并用真实 `{episodeId,taskId,revision,use}` 填 `episodes`，与内部审核选择一致；验证回合不冒充示范，`use` 不默认成功。

每组 `mapping.json` 含 `episodes=[{episodeId,revision,lerobotEpisodeIndex,hdf5Demo,steps}]` 和 `hdf5Obs={state:'low_dim',front:'rgb',...}`，再说明实际动作、时钟、权限、相机、标注与数据划分。不同组的回合不重复，总集等于本次已选回合。

先完成回读并计算清单摘要，交内部审核者检查；其最终 v2 回执顶层增加同一 `producedArtifacts={manifestRef,sha256}`。通过当前版本复审后，向既有 `project_deliver` 传相同指针，使用新 requestId。导出内容或清单变化需复审新摘要。缺必需格式或读取失败保持 incomplete；你修复导出后重新申请，原始执行和旧失败记录保留。只有唯一正式回执 accepted 后宣布本次请求的产物交付完成。
