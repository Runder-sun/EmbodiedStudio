# SceneAgent Studio 工程接口

本页描述原生扩展接口。`project_inspect.io`、冻结manifest和当前运行内核是实际合同；旧v4/v5包使用各自冻结worker及解释器，不能套用本页重建。模型导入、字段校验、数值规划和真实任务成功分别核验。

## 工程与树

工程按本次范围包含 `scene.py/tasks.py/rubrics.py` 和需要的冻结 helper、验证源；机器人数据阶段才需要行为程序及公共 `*.robot.json`。`World()` 默认原生、没有默认机器人；旧包使用自己的冻结 worker。构建、控制与评分在不同进程执行，正式运行不保留构建 Python 回调。当前 `scene_build` 要求非空 Task 和生成 rubric，尚无无 Task 的早期预览模式，不以虚假任务或恒真评分绕过。新型号、策略和 SDK 已有物理能力由工程配置、源与资源表达，不增加系统类别、适配器或任务工具。

```python
# scene.py：world.stage是本次原生构建的唯一stage。
def build(world):
    from pxr import UsdGeom
    world.space('room')
    UsdGeom.Xform.Define(world.stage, '/World/Entity')
    world.bind('entity', '/World/Entity', owner='room', purpose='按原文生成的实体')
    # 用当版SDK导入/构建真实模型、物理、材料，再声明实际I/O。

# tasks.py：返回Task列表。
def build(world):
    ...

# expert.py：仅机器人数据范围需要；只拿受控消息代理，不拿现场SDK句柄。
def run(env, task):
    contract = env.open()
    observation = env.observe()
    # 生成的策略/数值算法基于允许观测计算具名动作，经env.step执行。

# rubrics.py：私有生成计算，不写动作或宿主结果。
def evaluate(task, facts, state):
    ...
```

`world.stage` 已建立 `/World`、米制和Z向上。`world.resources[resourceRef]` 返回已冻结入口相对路径；构建当前目录是输出包根，可供原生USD引用或官方导入器使用。资源的网格、材料、配置和权重闭包均须准备并通过 `scene_build.resourceRefs` 指定；不把现场绝对路径或联网下载留给正式运行。

World 不自动补建物理场景。本接口的原生构建会核验实际 I/O：最终 stage 须保留一个实际 `UsdPhysics.Scene`，其 `PhysxSchema.PhysxSceneAPI` 的物理步频与 `world.io.clock.physicsRateHz` 一致，同时保持米制、Z向上。按最终 stage 检查已有场景后创建或复用，不能给每件导入资产各添一个；此要求不意味着必须加载机器人。

World 保留稳定ID、语义树、需求和原生prim绑定；真实几何、关节、驱动和物理属性以冻结stage为准。`owner` 是归属，`frame` 是坐标参考，支撑/容纳/连接另行表达；仅改归属不会移动或焊接物体。`world.bind(id, prim_path, owner=..., frame=..., purpose=...)` 绑定 `/World/...` 的真实prim，不复制一套物理状态；其姿态从原生读口获得，不能调用World.position猜动态位置。

现有 `space/box/cylinder/asset/light` 便捷函数仍可用，但不是原生表达能力的白名单。box/cylinder使用米制尺寸、at及wxyz四元数；asset使用准备报告的path/upAxis与统一缩放。既有 `collision_mode=box|mesh|convex_decomposition` 只属于这些便利几何，静态mesh/动态凸分解的实际精度仍需验证。原生构建可以使用当版SDK其他schema；机器人配置加载复用下述 `world.robot`，不按物理类别增加节点类型或任务工具。固定围护结构才使用 `role='enclosure'`，不能靠隐藏活动物体、删墙或关碰撞改善结果。

`box/cylinder.at` 是所选坐标参考中的几何中心，不是底面；父级变换也参与最终世界位置。`mass` 默认0且没有joint时是静态几何，不会因悬空自行落下。`supported_by` 只记录关系，不自动对齐、焊接或核真实接触；`owner/frame`、标签和结构validate通过同样不能证明物体已经放稳。原生引用模型的原点和支撑结构以实际stage为准，不按名称猜底面。

灯光可用原生UsdLux，也可用现有 `world.light(id,kind,intensity,at=...,frame=...,orientation=...,color=...,radius=...,width=...,height=...)`；kind为sphere/rect，分别给radius或width/height。显式灯光只用源声明，`lights=[]`关闭自动灯；强度是作者配置，非实测照度。网页GLB与RTX来自同一stage，不能保证不同渲染器逐像素相同。 构建回执的 `rendering.webPreview.available=false` 表示网页预览转换不兼容，具体原因在 `reason/limitations`；该版本不发布残缺GLB，原生场景仍可用 `scene_observe` 观察、用 `robot_run` 验证。预览缺失不是模型无效或任务通过的结论，用户要求的场景展示与质量检查仍须通过实际原生图像完成。

原生stage的外部依赖、动画和可执行脚本/图在构建隔离结束后、正式SDK打开前做一次被动核验。后续消费只核对已冻结报告与文件摘要，不重复解析和比较物理属性列表。未接线的运行脚本、Script/Graph节点和启动扩展不能借stage重载取得特权。不要把控制放进构建回调、timeSamples或直接pose写入；机器人示范只来自实际驱动执行。

当前原生stage须保持米制、Z向上。MDL可保留原生源与材质绑定，由构建中的官方SDK隔离审核递归imports、实际资源和有效参数，冻结后核摘要；路径引用按所属USD层锚定，裸 `OmniPBR.mdl` 等由锁定SDK解析。包内同名MDL可以有官方独立文件身份，不应一律换成SDK版本或转灰材质。目录旧记录中的MDL限制不代表当前全局禁令，以本次实际审核结果为准。当前尚未核验的连接输入图、时间采样输入、资源参数数组及非纹理资产参数会明确失败；不能用仅文件冻结声称任意材质图可用。SDK中的可选缩略图与渲染必需资源分别处理。

USDZ、MDLE/MDR容器、MaterialX及OSL仍保持原激活边界，不随MDL支持顺带放开。网页GLB仍只支持已核转换范围；原生可用而网页材质不支持时保留 `webPreview.available=false` 和原错误，继续用同版本 `scene_observe` 核实际RTX图像。`sourceResolution` 是官方来源与依赖复验；`rendererMapping` 的 `unknown` 表示当前未取得内部映射观察，不等于资产失败或BSDF等价已证。确需制作派生时保留原输入、转换源码、原/派生摘要和结构差异证据，不改原库，不把受限PBR映射说成无损MDL。旧 `World.when` 交互执行声明也明确拒绝，不能接收后静默忽略或恢复宿主任务宏；使用已核验的原生物理组件。

传给SDK导入器的输入路径先用 `Path(entry).resolve(strict=True)` 解析为已存在的绝对文件名，避免导入器在临时目录处理时丢失相对网格引用。构建中的 `/output` 是临时路径；导入器返回输出文件名后，把stage引用写成相对于所属USD层的路径，并核对各依赖层，不将 `/output` 或 `/inputs` 的绝对别名冻结进stage。原始资源只读，需转换时写独立副本。

## 资产与材料

按“优选仿真库 → 大规模本地库 broad → 联网找现成资产 → 按需生成”选择物体。普通物体也先用 `assets_lookup(libraryTier='preferred')`；没有合适候选再查 broad，随后才现场搜索，最后按需用工程源、LLM辅助或资产生成模型。默认 `all` 保持全库检索兼容。物类、用途、完整物体、尺寸和必要交互条件先满足，再比较外观；不能把零件当整物体，或让缓存、更多描述命中词压过合适的优选候选。缓存只是选定资产的当前准备复用，不是来源第0级。机器人、液体或软体等词不触发系统任务配方。

检索时把已知物类单独传给 `category`，`query` 补充用途、材质和风格；物类不确定时保留自然语言查询，再根据结果细化。中文或原库用词未命中时可用英文同义词重查，不把一次无结果当作库里没有。例：

```python
assets_lookup(query='wardrobe', category='cabinet', libraryTier='preferred', collection='artvip', requiresJoints=True, limit=6)
assets_lookup(query='dining chair', category='chair', libraryTier='preferred', limit=6)
```

优选目录来自当前数据根 `assets/collections/catalog.json`，与既有来源共用可重建索引。ArtVIP、RoboCasa365-Lightwheel、RoboDojo、NVIDIA SimReady精选及后续入库来源提供互补候选；实际物体、collection ID与版本以结果为准，不把清单当白名单或把未入库下载当可检索资产。`collection` 按实际ID精确筛选。`qualityEvidence` 区分源库抽样与本件观察，`importRequirements` 给出原格式和当前阻碍；有刚体或关节字段、来源优选、文件冻结都不等于操作已验证。BEHAVIOR加密场景/物体资产保持OmniGibson专用，不提取或转换为本系统独立Isaac资产；独立未加密的MIT机器人包按[机器人资源说明](robots.md)及实际包许可另核。

需要可动关节时，先在优选库中指定 `collection='artvip'`，再查其他优选库。`requiresJoints=true` 只匹配 `jointEvidence.status='source_inspected'` 且 `movableJointCount` 为大于0整数的候选；计数是已核内部非fixed可动关节，不包括固定或根连接。证据保留method/sourceRef/sourceRevision/sourceDigest，只证明该源版本的结构，不证明导入、驱动或任务操作通过。`requiresJoints=false` 或省略表示不要求关节；未知不会变成0。若严格过滤没有结果，去掉该参数继续查优选库，对少量候选补查源结构，不能据此断言库中没有关节物体。

先检索元数据，再对少量候选调用 `assets_lookup(assetId=实际ID)` 查看已在本地且哈希固定的本件来源图，无需先准备整个原生包。优先复用已有本件预览，包括来源核对后的官方单件图；有图不代表物体已由本系统看图或全部可用。标为 `publisher_render` 的图是发布方预览，不是本系统实际渲染。实际比较轮廓、比例、材质、细节与风格；源库样图不能代替单件观察，贴图不能当物体预览。缺图、未知尺寸或关节保持未知；USD/MJCF/SDF初查不强制 `textured=true`，以免排除贴图证据未知的原生候选。`textured` 只说明存在贴图证据，`visualReady` 只表示当前准备缩略图可看；候选选择与最终场景质量审核仍由生成器内部完成。

目录中的原生物体可直接 `assets_prepare(assetId=实际ID)` 保留完整源包目录、碰撞和关节文件，取得同一物体ID的准备报告，构建时传 `resourceRefs`。不静默取visual转成静态包围盒物体。MDL沿上述官方闭包审核原生加载，USDZ仍受原激活边界限制，MJCF/SDF是否有可用导入路径须核实际SDK与资源；目录准备成功只证明声明文件冻结，不能绕过检查或声称全部可用。候选质量和最终场景审核由生成器内部完成；同一资产多实例复用一次准备。

原生包的 `rigidReady/simulationReady/visualReady=false` 表示尚未取得该准备路径的对应验证资格，不是物体质量差或已导入失败；结合 `preparationStatus/importRequirements` 和实际观察判断。发布方单件图可用于选材，但不会自动取得本系统实渲资格。来源限制阻挡时，先判断是否能在现有支持范围内保留原输入并制作可追溯派生，或继续查其他现成候选；不要把一次无结果、缺预览或未核状态当成必须用粗糙方块替代的理由。自制前保留候选不适合的实际原因，最终仍核同样的造型、材质和物理要求。

原生资产摆放后，核对关节的 `body0/body1` 实际目标及 `localPos0/1、localRot0/1` 参考系：有目标时相对于该目标，无目标的一侧连接静态世界，不能当作关节所在父级的局部坐标。仅移动实例顶层 Xform 不保证世界侧锚点随之移动。外观符合而问题局限于变换、碰撞或安装时，先评估在现有支持范围内保留几何与材质、通过工程源校准接入，再以实际物理和图像复核；不一律删关节或固定全部物体，不改变任务所需活动能力。原输入保持只读，确实不适配时继续选材，不保证每件资产都可修复。

`assets_prepare` 四选一：

| 来源 | 参数与边界 |
| --- | --- |
| 本地索引 | `assetId=<lookup实际ID>` |
| 在线视觉模型 | `url=<公开自包含HTTPS GLB>,sourcePage,name,license`；由工作台主机下载，原始来源/未知许可保留 |
| 原生文件包 | `resourceFile=<仿真主机绝对入口>,dependencyFiles=[全部其他依赖],name,license`；可选 `mdlSearchRoots=[MDL模块根绝对目录]`；允许当前数据assets、只读legacy assets/data及锁定SDK前缀内同一公共源根，禁止读取其他工程或任意主机文件 |
| 生成源 | `sourceFile,expectedSourceHash,materialIds`；冻结 `build_asset(materials)` 和helper，不联网，输入材料显式冻结 |

原生文件包保留源根相对布局，报告为 `kind='native_files'`，返回entry/path/reportPath/digest/fileCount摘要；完整清单与来源在报告artifactRef。大文件不进入源码JSON或LLM消息。闭包显式声明并核摘要，不自动猜任意配置/权重中的依赖，也不执行权重。`filesFrozen` 仅为文件准备证据；这类包不会获得rigidReady/simulationReady/visualReady。

MDL 的绝对模块导入确实需要额外模块根时，才提供 `mdlSearchRoots`；严格文件引用或SDK内置材质不因此强制手填根。每个根须在同一允许公共源树内、包含显式声明的MDL文件；声明目录不扫描或复制整目录，包内及外部声明的模块、贴图和瓦片仍列入 `dependencyFiles`。锁定SDK自带模块由运行环境身份和实际源摘要单列复验，不要求复制整个SDK。集合已有MDL文件时复用其 `packageRoot`，特殊子目录可由集合元数据声明。准备报告 `inputs.mdlSearchRoots` 保存为 `files/...` 相对目录，构建随冻结包搬迁；源绝对目录不能当作后续运行依赖。文件冻结与模块根声明仍不代表官方材质解析或RTX外观已通过。

`project_inspect.runtimeExecution` 给出实际解释器与prefix。用原生终端按配置的remoteHost通过SSH只读查看已知远端资源路径，或查锁定版本的官方公开源码和文档；需要交付的示例或配置再用 `resourceFile` 冻结。不要执行随模型下载的脚本，也不要把导入示例中直接改现场状态的部分搬进运行器。

生成源保持Trimesh/Scene返回兼容；也可在 `/output` 写完整原生文件包，再返回严格的 `{'entry':'相对入口','files':['全部输出文件',...]}`。可选 `mdlSearchRoots=['相对模块根']` 放在此返回声明内，`.`表示output本身；不作为 `sourceFile` 模式的顶层工具参数。所有输出必须是目录内普通文件，列表与实际集合一致；不留未声明文件或链接。准备器的返回声明走独立通道，不占用原生包文件名。冻结报告保留源、材料及输出摘要；不同模块根有不同冻结身份，只有同源唯一有效缓存才直接复用，存在多个声明时由真实builder输出选择对应包。

当前 static_mesh 准备与 World.asset 便利路径要求三轴正尺寸，零厚度平面不属该路径的已支持范围；原生 USD 路径另按实际资格核验。视觉模型与材料继续按实际报告引用，`kind='material'` 可找材料。模型保留UV、多材质与贴图；材料可用于box/cylinder，`texture_period_m`表示一轮纹理的米制长度，不能和uv_repeat并用。在线模型先读来源页，再把公开自包含GLB的url/sourcePage/name/许可声明交给 `assets_prepare`；保留实际搜索和下载来源，无直接下载或须登录时另找资源，不能把网页当模型。付费、登录或确实缺必要信息时按原生边界处理。`currentPreparation` 要求当前版本、完整冻结文件及本地源依赖有效；旧报告或源文件变化不作当前证据。未知许可如实保留，预览不证明整场景或操作质量。

## 机器人配置

URDF原始连杆名可能被官方导入器转换成不同USD名。系统沿用官方名称/displayName映射；自动名称匹配有歧义时，生成器可用linkMap明确选择实际prim，不能用字符替换猜对应关系，歧义保持拒绝。

机器人选择及结构真实性遵循[真实型号候选与资源获取](robots.md)，本表不提供型号白名单或能力认证。配置表达真实模型与接入，不用于按任务改造机构。

接入新型号时新增一份工程内 `*.robot.json`，引用已准备的原生模型与规划资源。系统不查型号表，不根据机器人名字选择控制代码；配置描述模型与实际I/O，任务目标、约束、恢复策略、评分和语义标注由生成程序产出。多个机器人可共用一份配置，使用不同实例ID、安装位姿和端口命名空间。

接入顺序：准备模型及完整资源包，读取真实结构，编写配置；场景先设唯一 `world.io.clock`，再 `world.robot`；构建后读取冻结I/O，用同份配置创建规划器，做所需姿态的FK核对、短段实际执行与记录核验。错误映射、缺依赖和规划失败明确报错，不能降级成移动物体或修改评分。

以下展示配置形状；`resourceRef`须换成实际准备报告引用，prim路径、关节名及规划字段必须来自所选模型。不要照抄示例名称当实际结构。

```json
{
  "schema": "sceneagent-robot-profile-v1",
  "model": {"resourceRef": "实际模型包报告引用", "file": "files/model.usd"},
  "ports": {
    "arm": {"source": {"primitive": "joint_position", "path": ".", "joints": ["joint_a", "joint_b"]}}
  },
  "streams": {
    "q": {"source": {"reader": "joint_positions", "path": ".", "joints": ["joint_a", "joint_b"]}, "visibleToPolicy": true, "required": true},
    "dq": {"source": {"reader": "joint_velocities", "path": ".", "joints": ["joint_a", "joint_b"]}, "visibleToPolicy": true, "required": true}
  },
  "planning": {"resourceRef": "实际规划包报告引用", "file": "files/robot.yml", "assetRoot": "files", "ports": ["arm"]}
}
```

- 必填 `schema/model/ports/streams`；可选 `planning/initialJoints/drives`，未知字段、重复键、非有限数和非法路径拒绝。配置没有任务、评分、回调或SDK方法字段。
- `model.resourceRef`必须在本次 `scene_build.resourceRefs` 中，入口为 `native_files`；`file`省略用报告entry，否则相对该报告包根且须在nativeFiles清单内。模型支持USD/USDA/USDC或URDF；不从SDK示例目录隐式补依赖。
- USD用其defaultPrim，或显式 `model.prim` 绝对source prim；`model.variants`为实际variantSet到selection的字符串映射。源须米制、Z向上且根无resetXformStack，系统不隐式缩放。原文件只读，实例在本次输出stage内引用。
- URDF使用锁定SDK官方导入器，`model.importOptions`仅接受已核字段：`fix_base`为bool或null；`merge_fixed_joints/merge_mesh/allow_self_collision/collision_from_visuals/run_asset_transformer/run_multi_physics_conversion`为bool；`collision_type`为Convex Hull、Convex Decomposition、Bounding Sphere或Bounding Cube；`joint_drive_type`为force、acceleration或null；`joint_target_type`为none、position、velocity或null；`override_joint_stiffness/override_joint_damping`为非负有限数或null，`link_density`为正有限数或null。输入/输出路径由系统确定。网格和纹理须为包内显式依赖，未解析的package URI不自动搜索ROS目录。URDF不接受prim/variants，USD不接受importOptions。
- `ports`与`streams`复用原生I/O的source字段；path为相对本次模型根的真实prim路径，`.`为根，不能逃出模型。局部名称是简单标识符，实例合并为 `实例ID.局部名称`；SDK派生的shape、units、limits、origin不可自报。stream可给 `rateHz/visibleToPolicy/required`，缺省节拍沿实际I/O合同。相机和全局contact_report由场景另行声明。
- `initialJoints`为明确模型内joint路径到 `{position,velocity}` 的映射；`drives`同样映射到 `{stiffness,damping,targetPosition,targetVelocity}`，允许只给需设置的字段。均为有限SI值，增益非负；仅构建期在真实revolute/prismatic schema上设置，角度及角驱动增益转换为USD单位。它们不提供运行时改状态入口。

```python
# scene.py：实例位置归场景，配置可复用。
def build(world):
    world.io['clock'] = {'physicsRateHz': 200, 'controlRateHz': 50}
    robot = world.robot('arm_1', 'robots/model.robot.json', owner='room',
                        at=[0, 0, 0], orientation=[1, 0, 0, 0])
    # robot: id/primPath/mountPath/ports/streams/frames，全部是数据。
```

`owner`须为已有节点；`frame`可用已有静态节点或 `{'nativePath':'真实prim路径'}`，安装位姿仅在构建期应用。`primPath`为模型实例根，`ports/streams`返回局部名称到限定名的映射；不要猜系统编码的prim名。若需把返回映射用于expert，可由tasks冻结到task.parameters，不能依赖构建进程对象持续存在。

`planning`包含 `resourceRef/ports`；配置文件方式给包内 `file`（省略用entry），安全读取原生cuRobo YAML的robot_cfg或其内部字典；`assetRoot`默认YAML所在目录，显式值相对报告包根。新编规划参数也可直接给 `planning.config`，其值就是现有 `create_planner(dict)`接受的原生cuRobo内部配置（含kinematics），与file互斥；此时不读取entry为YAML，assetRoot缺省为规划资源包根。两种入口使用同一依赖闭包、关节和限位核验。实际URDF/USD、网格、asset_root和collision_spheres须在该冻结包清单内，不能回退SDK资产。保留原生布尔开关；实际数值字段不能用bool冒充。

`planning.ports`选择本求解器控制的局部位置端口；默认仅接受规划关节与实际关节唯一同名的完整双射。名字不同时显式 `jointMap={规划关节名:{'port':局部端口名,'joint':真实关节名}}`，不按数组长度或顺序猜。`planning.linkMap`可给规划base/tool名到模型内真实prim的相对路径，否则只接受唯一同名prim。锁定关节仍只是数值模型假设，expert须核其实际q，不会自动施加物理锁。

必要 base/tool 由真实模型解析，`frames[link]` 返回真实 body pose 的 `stream/bodyPath` 及静态 `bodyFromLink`（4×4，列向量约定）。生成器须在工程 I/O 或机器人配置中显式声明同 bodyPath、来源和节拍合格且唯一的真实 pose 流；规划器不会自动新增公开别名，也不改写该流的 `visibleToPolicy/required`。隐藏流仍可绑定 frame，但策略只有获授权才可读取它。缺失、歧义或无刚体祖先时报实际路径。工具坐标按 `worldFromBody @ bodyFromLink` 计算，属于真实刚体流的固定帧换算，不是原生直接 TCP 读口；所用姿态仍需核 FK 与实际执行误差。

`*.robot.json`明确属于公开模型数据：修改即改变sourceHash，构建和expert自动带上当前工程全部该后缀配置；不得将私有rubric/case数据、评分状态或未来观测放进去。`sourceRef`仍只能是Python，helperRefs可显式带配置数据；普通JSON不会自动冻结。`world.robot`相对路径只从本次冻结源根读取；expert以 `Path(__file__).parent / 'robots/model.robot.json'` 定位，或传显式source_root，不按cwd搜索。

配置可扩展真实型号的已有USD/URDF模型、具名端口和已有原始读口。它不能产生缺失的算法、设备驱动或未知传感器读口；人形/移动机器人仍需匹配的控制策略。已有SDK和控制/读取原语足够时，生成器可只改工程配置、资源与策略完成扩展；底层原语缺失时保留缺项并报告。

## 任务、控制与观察

`Task` 仍包含 `id/instruction/bindings/parameters/rubric/measurements/evaluation/recording/execution`。bindings把生成角色映射到真实节点；parameters表达生成语义。`rubric={'sourceRef':'rubrics.py','helperRefs':[...]}` 选择独立评分闭包；适用 `evaluation.cases` 每项为 `id/sourceRef/helperRefs/expected/requirementRefs`，由生成器设计有区分力的实际案例并独立内审。先robot_run运行，再task_check引用同版episodeIds；程序错误不冒充正常失败反例。

`expected` 从 `success/terminated/reward/details` 选择非空子集，按选中的整个字段 JSON 值精确比较：success 为 bool 或 null，terminated 为 bool，reward 为有限数或 null，details 为有限 JSON。当前不提供 `rewardMin/rewardMax` 或跨回合排序操作符，不要自造字段；具体声明校验见 `runtime.scene.Task.validate_evaluation`，比较见 `runtime.worker._case_matches`。案例期望仍须独立于待测评分产生。

`caseId` 选择冻结验证案例并把回合绑定到其期望，不会自动注入 `env.case` 等策略参数。需要不同控制行为时选择相应的冻结验证源，源程序可共用 helper；同一验证源也可供多个案例检查不同输出字段。

原生数值测量由唯一io.streams声明；Task.measurements仅保留 `camera={'cameraIds': [...], 'visibleToPolicy': True|False}` 的本任务相机权限，未知旧测量渠道会拒绝。

### 原生I/O与受控动作

场景声明 `world.io={'clock':...,'ports':...,'streams':...}`，构建从实际SDK对象核关节、驱动模式/目标、限位、维度、单位和来源，冻结为唯一 `sceneagent-native-io-v1`。不再并行维护Franka动作切片或另一份actionSpec/observationSpec。

```python
def declare_io(world, articulation_path, actual_joint_names):
    source = {'path': articulation_path, 'joints': actual_joint_names}
    world.io = {
        'clock': {'physicsRateHz': 200, 'controlRateHz': 50},
        'ports': {'arm': {'source': {**source, 'primitive': 'joint_position'}}},
        'streams': {
            'q': {'source': {**source, 'reader': 'joint_positions'},
                  'rateHz': 50, 'visibleToPolicy': True, 'required': True},
            'dq': {'source': {**source, 'reader': 'joint_velocities'},
                   'rateHz': 50, 'visibleToPolicy': True, 'required': True},
        },
    }
```

以上名称和频率仅演示声明形状；真实路径、关节与驱动来自模型。可用原语为 `joint_position/joint_velocity/joint_effort`，分别要求真实PD、速度或零增益effort驱动，不能改模式名字绕过核验。多个端口可属于多个原生Articulation；同一实际actuator不得重复绑定。端口模式随版本冻结，位置/速度目标可在已初始化且实际持久时省略；effort每个请求必须显式提供，并由运行器按物理节拍重发。未知目标、别名冲突、不符合端口命令包络或非法维度在推进前失败，不暗填HOME/0或用当前q猜旧目标。

position端口的`limits=[[null,null],...]`描述SDK期望驱动参考的未设界域；`physicalJointLimits`另存实际关节stop，二者不是同一含义。策略的参考可超出物理stop，不能因此假拒或静默clamp；数值规划仍使用真实物理模型范围，实际姿态与效果另测。velocity/effort的limits暂为按原生配置给出的保守工具命令包络，不声称SDK目标buffer本身有此硬界。`nativeDofIndices/nativeDofPaths`保留实际映射，不靠名称或维数猜顺序。

`env.open()` 返回 `io/resources/compute/resetSpec/sensorSpec/observation`。resources的键仍是准备报告引用，值为隔离进程内 `/resources/...` 只读入口；compute中的cuda:0是已分配单卡的局部编号，不是任选宿主GPU。纯计算helper可复用锁定库与权重，无现场stage、网络、其他工程或共享可写JIT缓存。

`env.observe()` 不推进物理，返回时钟、`streams/streamStatus/cameraFrames`；每个流按 `visibleToPolicy` 授权，不自动公开评分事实。大数组在唯一提交边界后变为 `{'ref':'arrays/<sha>.npy','sha256':...,'dtype':...,'shape':...}`；策略用 `runtime.records.read_array(contract['sensorSpec']['arrayRoot'], ref)` 核摘要并只读加载，不读整个episode或未来数据。当前arrayRoot为 `/sensors`，不存在的引用不补值。

`env.step({'端口名': 数值数组}, seconds=None)` 是唯一物理动作入口，默认一个冻结控制周期。单请求时长上限为 `max(0.1, 1/controlRateHz)` 秒，须为非 bool 的有限正实数，不接受字符串，并对应完整控制周期；仍受回合剩余物理预算约束。全量校验后写原生驱动，记录请求、实际读回目标及原始状态；effort 读回是 actuation buffer，不是实测力矩。终止/截断后停止剩余周期，失败保留实际前缀；记录后重置须开新回合。

冻结环境的 `runtimeExecution.controllerOutputContract='separate-logs-v1'` 表示控制程序导入及运行时的普通 Python/C 标准输出已与动作协议分离，日志进入诊断流，不会被解析成动作；`env.open/reset/observe/step` 和真实动作记录不变。没有该标记的旧冻结运行器保持原实现；需要这项通用修复时可在允许构建的新工程中冻结同源新版本，保留旧场景与执行记录。该能力标记不证明机器人任务成功。

executedAction是本次实际发送且读回的端口；省略的持久端口没有伪造一次写入。nativeInitialTargets是初始化时的实际读回，active_targets缓存则由它与后续指令推导；后者不代表每周期全目标实测。需要逐周期核全部有效目标时，显式声明joint_position_targets/joint_velocity_targets等原始流。

`Task.execution` 只声明 `physicalSeconds`，由生成器按任务设置为非 bool 的有限正数，不设统一最大值；省略 `Task.execution` 时默认 30 秒。运行器内部控制器与单次评分保护分别最多 120 秒和 30 秒，任务声明的物理时长仍决定回合截断。现有 Kit 作业工具接受可选 `workerTimeoutSeconds`：省略时沿用 900 秒，显式值须为非 bool 的有限正数，不设固定最大值。它约束本次 Kit 启动、执行、子进程文件核验和正常 SDK 退出；超时后的有界清理宽限、退出后的父进程独立核验及发布另计。它不改变 physicalSeconds、控制器或评分保护，也不是生成任务总预算。

`workerTimeoutSeconds` 属于本次调用参数，不写入 Task、机器人配置或物理场景；调整它不要求修改生成源。需要兼容冻结包的外部评测器时，显式参数须有 `manifest.runtimeExecution.workerTimeoutParameter == 'workerTimeoutSeconds'` 能力标记，旧包不支持时明确拒绝，不能静默忽略。缺省调用保持原请求身份；同 requestId 的重试核对原作业，调整参数的新尝试使用新 requestId。实际有效时限在新作业回执和 performance 中披露。

空端口仍可构建、运行和记录被动物理环境；这类结果不能获得机器人示范资格。机器人示范至少有实际核验的原生控制端口；持久端口已初始化时可用空命令保持目标，具体任务是否完成仍由生成评分和内部审核判断。

### 独立阶段与布局集合

`videoOnly=true` 的新录像工程若没有锁定 `environmentRefs`，允许在本工程准备并 `scene_build` 所需环境，然后仅交 demonstrations 视频。显式选择了已有环境时仍只读复用、禁止重建；下面通常采集入口的已有环境限制不适用于前述新录像工程。

当前新构建声明 `capabilities.stageSplit/independentBehavior/robotSetup=true`。宿主在生成启动时固定唯一 `requiredOutputs`：完整三项、`[generation,evaluation]` 或 `[demonstrations]`。纯场景范围不选型、准备或加载机器人，不生成或运行专家采集；保留必要家具关节、被动物理、评分与接口验证，允许空动作 `env.step`，环境交付不以前置机器人任务成功为条件。采集新任务先选择已有环境所属工程并锁定 `environmentRefs=[{revision,split?,taskIds?}]`；`split` 为可选 train/validation/test 标签，`taskIds` 是明确采集子集。场景/评测交付仍覆盖本次要求的全部冻结任务，不由审核者删选规避。

`scene_build.environmentSourceRefs` 为实际环境源闭包：含scene.py/tasks.py及它们、rubric、case所需的源文件，仅含机器人时才带其配置。只把这些文件送入隔离构建；行为入口可不在其中。省略时仍兼容旧全源输入。机器人阶段传 `behavior={sourceRef,helperRefs}`，宿主从当前工程冻结精确代码闭包，运行器在同工程内容地址下保存独立behaviorRef；每回合绑定环境revision与该行为身份。行为不得读取私有评分/case源。`repetitions` 为同一布局重复次数，不与seeds同时使用，也不等于新布局数。

已锁定已有环境的 `[demonstrations]` 禁止 `scene_build`。给无机器人的兼容环境添加机器人，使用同一个 `robot_run`：

```python
robot_run(revision=base_revision, taskIds=task_ids,
    behavior={"sourceRef": "behavior.py", "helperRefs": []},
    robotSetup={"robots": [{"id": "arm", "profileRef": "models/arm.robot.json",
                            "at": [0, 0, 0], "orientation": [1, 0, 0, 0]}],
                "resourceRefs": [prepared_report_ref]},
    purpose="demonstration", requestId=stable_request_id)
```

`profileRef` 是当前工程的真实 `*.robot.json` 文件；`resourceRefs` 是 `assets_prepare` 返回的准备报告，不能互换。robots 可包含多个具名实例，支持可选 `owner/frame/purpose`，位置与 wxyz 单位四元数使用原环境坐标。复用已有配置加载器，不按型号或任务写分支；模型、算法及实际接口仍须可用。配置源与资源闭包由工具冻结，安装输入保存在 `robot-setups/<摘要>/`，实际回合返回 `robotSetupRef/robotSetupSha256`。以后调用传互斥的 `robotSetupRef` 复用，不必重新安装；改变位置或配置产生新的安装输入，行为可独立改变。原 base revision、生成任务定义、rubric、原 manifest 与交付回执不变；同一 base 的不同安装按各回合的引用读取，不能只按 revision 合并 I/O。

可选 `robotSetup.recording={taskId:完整recording声明}` 为任务追加实际录制视角；原相机声明须原样保留，已有 primary 不变。腕部或机身相机按真实 link/frame 和需要的 pose 流声明，仍经原生接口和标定验证。增加外部录制视角不会自动扩大原策略的 `measurements` 像素权限。`scene_observe(episodeId=..., cameraId=...)` 读该已发布回合的真实相机；独立评测 `examples/evaluate.py --robot-setup-ref <同一引用>` 使用相同安装输入，低分不撤销原环境回执。

旧包未声明 `capabilities.robotSetup` 或 runtime 不匹配时明确保留兼容缺项；兼容新构建属于另行明确的场景任务，不在只采集任务中无声重建或修改 manifest。独立行为本身仍只替换控制源，不承担模型安装。

不同布局用同一生成源按不同构建seed冻结独立revision。`project_deliver.environmentRefs` 可选择多个同工程布局；新合同的内部审核回执和导出清单binding均原样包含 `requiredOutputs/environmentRefs`，`coverage.episodes` 每项为 `{episodeId,revision,use}`。只场景时可为空，交示范时须对应当前采集任务的真实回合。接口验证使用已核验的同版本同任务case真实动作，固定为consumer_diagnostic，不要求成功专家也不获得示范资格。

消费报告是数据集引用清单；每个布局的任务、I/O、评分与实际回合独立关联。同版本不能同时标成不同split；标签本身不证明多样性。仅采集不重新消费未请求的LLM/环境包；仅场景不要求机器人双格式。正式数据导出见[自主交付](delivery.md)。

### 复用运动规划与策略

生成的expert/helper可以直接调用：

```python
from runtime.motion import create_planner, plan_trajectory

def numerical_plan(robot_config, world_config, start, targets, control_period):
    planner = create_planner(robot_config, world_config, interpolation_dt=control_period)
    return plan_trajectory(planner, start, targets)
```

`create_planner`保留原有dict入口，也接受上述配置路径：

```python
from pathlib import Path
from runtime.motion import create_planner, plan_trajectory

def configured_planner(env, task):
    contract = env.open()
    return create_planner(Path(__file__).parent / task.parameters['robotProfile'], {},
        resources=contract['resources'], io=contract['io'],
        port_map=task.parameters['robotPorts'],
        interpolation_dt=1 / contract['io']['clock']['controlRateHz'])
```

这里的robotPorts须为 `world.robot(...)['ports']` 冻结进任务的映射；world_config仍由expert基于允许观测构成。配置入口返回原生MotionGen；`plan_trajectory`额外返回 `portJointIndices` 和 `profileProvenance`，前者把每个实际端口映射到返回position行的索引，按其生成具名命令。起点和结果核实际 `physicalJointLimits`，不会把未设界的驱动参考当物理范围。原dict入口及其返回兼容。

返回的规划器仍使用锁定cuRobo的原生方法及参数类型。例如当前 `planner.update_world(...)` 接受 `curobo.geom.types.WorldConfig`，更新时用 `WorldConfig.from_dict(world_config)` 转换，不能因为 `create_planner` 接受dict就推断所有原生方法也接受dict。其他原生方法同样先核对实际签名。

robot_config/world_config使用冻结原生模型及当前允许观测构成的cuRobo配置，不硬编码型号。start必含 `{jointNames,position,velocity}`，可显式给acceleration/jerk；未给的导数只作为数值边界条件0，并在unprovidedDerivativeAssumptions披露，不能冒称实测。关节顺序等于实际planner.joint_names；targets为 `{真实模型link名:[x,y,z,w,x,y,z]}`，含模型的主末端，可给多个末端共同求解，坐标为该模型基座。返回 `kind='numerical_plan',actualExecution=False,jointOrder,timebase,position,velocity,targets`；失败没有隐式降级。空world_config表示没有世界碰撞检查器，不造远处假障碍物，也不因此取得环境避障资格。

规划不推进仿真，也不提供宿主env.motion RPC。同一次行为运行可复用规划器，尝试多个符合约束的数值目标；只对通过筛选的候选推进真实物理。世界或持物关系变化后更新碰撞模型和起态，不跨状态盲用旧轨迹。

配置入口的计划额外带原始 `start` 和 `executionBinding={clock,ports}`，可由同模块的 `execute_trajectory` 执行。旧 dict 入口仍可用于数值计算；缺执行绑定时，新执行函数明确拒绝猜测端口。

```python
from runtime.motion import execute_trajectory

result = execute_trajectory(
    env, plan,
    feedback=task.parameters['motionFeedback'],
    start_tolerance=task.parameters['motionStartTolerance'],
    tracking_tolerance=task.parameters['motionTrackingTolerance'],
)
```

`feedback` 为 `{实际端口名: {'position': q流名, 'velocity': dq流名}}`，覆盖所有规划端口，流可含额外关节但须指向相同真实路径及关节，授权为策略可见、原生输出且在当前时刻更新。`start_tolerance={'position': [...], 'velocity': [...]}` 与 `tracking_tolerance=[...]` 都按 `plan.jointOrder` 顺序给出非负有限逐关节容差，使用该机器人的 rad、m 及速度单位；由生成器结合真实构型和控制条件确定，不提供统一型号阈值。

函数在发送动作前核验完整计划、端口身份、物理关节范围、时钟、起态 q/dq 和数据权限；不自动重采样或重定时。第 0 点只校验当前起态，N 点推进 N−1 个完整控制周期，所有规划端口每周期同时下发。未规划端口只沿用已初始化的 persistent 目标；必要时调用方可用 `hold_action={非规划端口: 完整数组}` 显式保持，不补零或自动松爪。随后直接检查每次 `env.step` 返回的已提交观测，不重复 observe；大数组按授权 arrayRoot 用 `read_array` 读取。

排查起态错误时分别保留：求解输入的真实q/dq、计划第0点的q/dq、执行前当前q/dq，以及本次已声明容差。抓握、等待或其他真实步进后重新取得实际起态；不把仍在运动的关节速度写成0。先区分求解器边界、陈旧观测和实际跟踪问题，不删除预检，也不用任意放宽容差掩盖不一致。

返回 `kind='trajectory_execution'`、`status=completed/terminated/truncated`、`requestedCycles`、`returnedStepCalls`、`confirmedPhysicalSteps`、`totalPhysicalSteps`、`countStatus`、`lastConfirmedWaypoint`、`lastObservation`，没有任务 success。物理步数相对此次执行起态，回合绝对步数仍在观测 physicsStep。起态终止或截断时不发送动作；跟踪越界或反馈异常停止后续下发，抛出的 RuntimeError 带 `execution` 已确认前缀，保留控制进程原异常 cause；env.step 异常后的实际尾部保持未知，不能将返回次数换算成精确执行步数。预检错误不发送动作，缺文件等真实 I/O 错误不伪装成功。

这一合同借鉴 [ASPIRE 通用轨迹执行](https://github.com/NVlabs/ASPIRE/blob/fa00f0ef2bc1d91d594b9aa502c22133c9f538f7/src/cap_harness/api.py#L882)，本实现复用现有原生 I/O，无新增 RPC、任务动作解法或 ASPIRE 运行依赖。它不提供每物理子步的在线避碰、动态平衡、失败回滚或液体存取能力。生成程序仍可按实际接口自行执行和发展恢复策略；编辑或重采样计划不自动继承求解器保证。物理抓握、稳定释放、避障和任务成功由实际执行及生成评分核验。双臂共同模型/多末端配置不等于已经验证联合任务；人形行走应复用匹配策略的观测、动作、归一化、节拍和reset，通过同一步进执行，不能固定根/腿或移动根姿态冒充。

### 原始读取、液体与生成计算

当前已接线原始 reader：

| reader | source附加字段 | 数据与边界 |
| --- | --- | --- |
| joint_positions / joint_velocities | path,joints | 实际关节顺序的q/dq |
| joint_position_targets / joint_velocity_targets / joint_efforts | path,joints | 原生驱动目标/指令buffer；efforts不是实测力矩 |
| root_pose / root_velocity | path | 原生Articulation自由根状态，含实际世界姿态/速度 |
| rigid_pose / rigid_velocity | path | 原生RigidPrim状态 |
| particle_state | path,field=positions或velocities | 独立粒子集的原生仿真数组，当前仅恒等world frame |
| contact_report | path=物理Scene,actors=显式RigidBody路径列表,field=headers或points或impulses | 实际SDK接触报告；空报告不证明分离 |

pose按xyz+wxyz、速度按线速度+角速度；流元数据的dtype/shape/units/frame/rateHz/origin来自实际绑定。未接线的传感器/状态明确 unavailable，不允许作者提供getter或facts字典自称原生测量。未接线的读口不能从属性名或无报告推断。rateHz为正整数且整除physicsRateHz；高于控制率的流在physicalSamples逐真实物理tick保留到期子集，未到期不补值，sampleTime/physicsStep保持实际采样时间。低频缓存不改策略的控制节拍。 `required=false`允许成功绑定后的采样暂时不可用；初始化仍须取得真实dtype/shape等元数据，不表示可以跳过读口绑定。

contact headers为uint64的13列，按冻结columns解释：`eventCode,actor0NativeId,actor1NativeId,collider0NativeId,collider1NativeId,proto0,proto1,offset,count,actor0PathIndex,actor1PathIndex,collider0PathIndex,collider1PathIndex`。NativeId列1–4是进程内原始ID，不跨进程直接相等比较；列9–12索引冻结actorPaths中的稳定USD路径。offset/count选择同批points/impulses的真实行，stage身份逐样本核；不能把这些ID当语义标签，也不能从未报告接触断言所有交互已分离。当前公开points/impulses是世界位置m与原生冲量N·s三列，不虚构未提供的法向量/受力侧资格。

液体在scene.py用固定版本SDK的原生PBD构建方法表达，读取该版官方例子，核GPU物理、粒子系统、材料、实际容器碰撞和读回配置；不增加World.fluid或倒油模块。粒子初始points/velocities用世界坐标，粒子集经祖先变换合成后的world矩阵必须持续恒等（容差1e-9）；不把粒子集挂在移动瓶/刚体上随动，容器通过真实碰撞带动流体。构建源不得预写simulationPoints；读口要求已启用的set/system、唯一GPU物理owner及已发生的真实物理执行。原始位置按SDK有效配置读取：流体粒子且粒子平滑实际启用时取simulationPoints；无平滑、平滑禁用或固体粒子取SDK同步后的Points.points，不把禁用平滑后残留的simulationPoints当当前状态。速度独立读取Points.velocities。读回设置按请求字段和实际后处理检查；不能把未启动、关闭读回或源程序初始数组冒充真实输出，也不要求所有粒子强制开启平滑。原始位置/速度与质量配置分开：区域质量、体积估计、洒漏及质量守恒由冻结helper/rubric基于原始粒子和明确质量配置计算，记录输入、公式和误差，可重算但不冒充原生直接测量。MassAPI.mass是该粒子集的总质量配置，不是逐粒子实测质量；仅明确等质量、粒子数封闭且不增删时，才能用总质量/N及实际区域计数计算区域质量。density配置需有相应SDK解释或标定，不能自动猜出总量。任意USD自定义属性是作者配置，不能自行提升为物理真值。原始粒子运动的探针不证明容器承液、油液参数、实际倒液或完整任务已通过。

物理材料与视觉材料分别配置：PBD的密度、黏度、表面张力决定物理行为，不会自动生成有颜色、透明度或反光的液面。原文需要可见液体时，在同一 `world.stage` 选择与当版SDK及现有材质资格相容的表现方式；原生粒子等值面与 `UsdShade` 材质绑定是可选构建入口，不是所有粒子场景的强制配方。可见表面须随同一回合的真实物理粒子更新，不能用另写动画、静态液面或另一回合影像冒充实际流动。数值测量仍使用合格原始粒子流，不从渲染表面反推物理真值。原理见[官方流体渲染说明](https://docs.omniverse.nvidia.com/kit/docs/omni_physics/latest/dev_guide/particles/particles.html#post-processing-for-fluid-rendering)，接口以当版安装源码为准。

当版实现从安装包的 `extscache/omni.physx-*/omni/physx/scripts/particleUtils.py` 查 `add_physx_particle_isosurface(stage, path, ...)`，它将 `PhysxSchema.PhysxParticleIsosurfaceAPI` 应用到粒子系统prim；格距、表面距离和网格预算按粒子尺度与实际效果选择，不照抄其他场景参数。官方 `extscache/omni.physx.tests-*/omni/physxtests/tests/PhysxParticleIsosurfaceAPI.py` 展示生成表面、更新和粒子显示切换；这些路径已核对PhysX扩展110.1.11，换版重新确认。视觉着色可用已支持的 `UsdPreviewSurface` 与 `UsdShade.MaterialBindingAPI`，物理绑定与视觉绑定用途分开；启用等值面或设置透明度本身不证明液面已经生成、正确着色或实现真实折射，仍看实际RTX输出。

当前隔离实测通路为 `UsdGeom.Points` 流体粒子、原生等值面和 `UsdPreviewSurface`，未启用可选的粒子平滑，原始位置/速度与RTX影像由同一记录器保存。粒子平滑与等值面同时启用的测试组合缺少合格的原始位置读回，尚未通过；这不构成所有场景禁用平滑的规则。可按原文选择不依赖该后处理的表现或验证其他合法组合，原始读口不可用时保留缺项，不预写输出或回退未经来源核验的数组。此通路只验证动态表面与记录链，不代表油液物性、稳定承液或拟真外观合格。

在已有内部审核中按原文核对关键动态效果与结果是否在实际影像中可辨，结合原始状态和帧表检查来源与时刻。视角、分辨率、采样频率、材质与灯光由生成器按用途选择，不固定所有任务的画质参数，也不要求每个内部物理量都可见。模糊、遮挡或外观未配置时保留相应缺项并自主修正；粒子运动、数值成功或视频可解码不能单独证明写实效果。普通可见材质或局部表面测试也不证明折射拟真、液体物性、容器承液或完整任务已验收；MDL等边界仍见[原生材料限制](#工程与树)。

同样方法可用于SDK已有软体、绳索或新传感器：工程负责原生构建与生成算法，系统只在缺原始读写原语时补一次可信接线；统一接口不保证所有物理能力都已经接通。

### 录制相机、策略像素与语义

`Task.recording` 仍只有 cameras 和 primaryCameraId。每路完整声明 cameraId/frame/at/orientation/resolution/rateHz/modalities/video/required/source；frame 用 world、场景节点或 `{'nativePath':'/World/...实际挂载prim'}`。位姿为米制 at 与单位 wxyz，实际外参随挂载逐帧核验。当前 nativePath 即使指向看似静态 prim，也要求一条唯一同 path `rigid_pose` 流（shape=[7]、frame=world_wxyz、origin=native_output），rateHz 为 cameraHz 整数倍且各帧时刻有真实样本；不能用 root_pose 或初态矩阵替代。固定相机使用 world 位姿。H.264/yuv420p 仅对 `video=true` 要求偶数宽高；`video=false` 保留原分辨率，系统不裁切或补像素。

多视角与分辨率按任务需求配置；当前相机采样在控制边界，rateHz须不大于controlRateHz且整除它。source区分simulation_observer与simulated_robot_sensor，不能称为实机相机。required视角缺失不获得完整媒体资格，optional故障保留真实前缀；图像、标定和时钟只经Task.recording与cameraFrames/frames提供，不声明camera_frame数值reader。策略权限仍由Task.measurements.camera.cameraIds及visibleToPolicy授予；同名cameraId按Task区分，不跨任务猜首个相机。未授权、未来或其他回合图片不进入策略。策略按open.sensorSpec.root与frame.path读取只读已提交RGB，不内联base64。

trajectory.frames 是唯一帧表，index 为全局行号，cameraId/frameIndex 为相机内编号；时钟、像素 SHA、内参及 worldFromCamera 逐帧保存。scene_observe 的 frame 用全局 index，不能与相机内编号混用。无 episode 的 cameraId 当前只在第一冻结 Task 中解析，省略时取其主视角，不能跨 Task 猜同名相机。`nodeId` 子树取景不能与 cameraId 或回合帧混用；读实际 cameraScope/viewMode/hiddenVisualNodeIds，不把剖视或一次默认镜头当全场景验收。

同版静态场景可用 `scene_observe(cameraIds=[...])` 在一次作业内观察第一冻结 Task 的 1–4 个互异冻结相机，返回 `observations`，每项沿用单图的像素、相机、时钟、范围和来源字段。工具回执中的每项另有 `imageContentIndex`，直接索引本次回执的 `content` 数组（0 为文本，图像从 1 开始）；相同像素可共用同一图像块，按该索引绑定相机，不按去重后的图块顺序猜。该参数不能与 `cameraId/nodeId/episodeId/frame` 混用；旧单图与回合帧接口保持原义。4 是单次响应的批处理资源预算，不限制 Task 声明的视角数，更多视角可分批；任一请求相机未产生真实帧则本作业失败并保留诊断，不省略视角冒称齐全。批内帧核对同一 stage、物理步与仿真时刻；这仍是场景观察，不能替代机器人实际执行或原文所需的质量审核。

语义来自原文、Task.bindings与冻结实体来源。生成评分可在details.annotations给出 `{'id':...,'text':...,'bindings':['任务角色']}`；记录器按已提交事实保存generated_interpretation及周期、源码和事实引用。标签不证明物理成功；当前录制模态只接通RGB，没有深度或像素实例/语义mask；文字语义不是这些像素数据，未接原生annotator时保留缺项。

### 生成评分与三产物

新任务若声明 `generation.deliveryContract='producer-exports-v1'`，由生成器按本次 `generation.requiredOutputs` 在独立 `delivery/` 目录产出所请求的生成轨迹、机器人数据或仿真评测目录及清单；官方工具、数据语义、路径结构和清单字段只在[自主交付说明](delivery.md)维护。导出程序不进入物理 sourceHash；修改格式后可自主重导，复用已选真实回合。`project_deliver` 增加 `producedArtifacts={manifestRef,sha256}` 指向 `projects/<projectId>/delivery/<run>/manifest.json`；系统读取标准格式和已有物理消费者，不执行导出脚本或代修字段映射。未声明此契约的旧任务沿原三产物门，不自动升级。

单入口 `evaluate(task,facts,state)`；phase为reset或transition，state初始None、仅有限JSON。reset含唯一initialSnapshot；transition含实际周期的observation/nextObservation、targetAction/executedAction、truthBefore/truthAfter及适用physicalSamples。数组ref通过当前评分私有只读工作目录加载：`np.load(ref['ref'], allow_pickle=False)`，只挂本次facts允许的数组，不挂/sensors或整episode。派生helper不得向策略泄漏评分私态。

```python
# 返回结构示意；具体任务语义由生成源码决定。
{'state': None, 'result': {'success': None, 'terminated': False, 'reward': None, 'details': {}}}
```

success可为True/False/None，terminated独立，reward有限或None；未知事实不填0/False。Recorder是唯一事实与动作提交边界，评分预览、fsync及索引提交完成后才回复；评分或提交故障保留实际前缀及未提交尾部。父级从初态与空评分私态重算同一冻结程序，关闭不再次评分。

把原文中的必要过程、同一实体和先后关系分别落实到 `Task.bindings`、真实 `facts` 和跨周期 `state`：先固定操作对象与目标的稳定绑定，再按已发生的事实推进阶段；终态位置或低速不能替代未发生或未证实的必要过程。主动设计“终态位置和低速相同，却因身份、顺序、承托或原文要求的释放未成立而不应成功”的反例；只检查本任务必要条件，不把这些例子变成所有任务的前置。

必要关系不能只靠邻近点或标签认定。确需接触证据时，用现有 `contact_report` 的 headers 路径索引查冻结 `actorPaths`，核实际物体/目标碰撞对，再按同批 `offset/count` 关联 `points/impulses`，结合适用几何与跨周期事实判断；双方身份或接触事件不独自证明承托或释放。空报告不证明分离，冲量不是实测力；当前读口不提供法向或实测力，不虚构这些字段，不设通用接触或稳定阈值。原文需要释放时，按实际机器人/工具配置的可观测性选取事实；若真假情形在现有读数中不可区分，阶段标签或state不能补出证据，该必要项保留未核；不能注明“近似”后仍把必要目标记为成功。纯场景交付不因此新增机器人、专家或成功示范前置。

合成 `facts/state` 单测检查评分分支和上述反例，真实接口与物理记录核读口、绑定和所需响应，两者分别说明；合成输入不冒充现场事实或实际完成。冻结案例按任务选择行为、期望、时长和观察范围，不要求所有任务采用同一案例集或固定推进时长。

请求机器人数据时，v6记录以同一I/O合同保存具名动作、流和数组侧车，批读取按时间配对，不展平为固定机器人向量。当前消费报告为 `sceneagent-product-consumption-v1`：按本次范围核所请求的生成公开轨迹、原次实际行为数据或评测环境，以及各自适用的案例与接口。同份记录的事实/动作/评分一致性严格核验，不强制跨次物理状态逐值复制或默认二次成功。外部候选通过匹配包的examples/evaluate.py独立运行，低分/错误不改变原环境的交付回执。

## MCP 工具

所有调用有公共 `projectId/generationId`；会话配置了固定上下文时可以由 MCP 填入，否则使用 Studio 提供的真实标识。不要猜标识。JSON Schema 随 `tools/list` 返回，是参数形状权威。

| 工具 | 用途与关键字段 |
| --- | --- |
| project_inspect | 返回原文、requirements/version/hash、源码路径/hash、capabilities、jobs、缺项；可选 nodeId/baseRevision |
| assets_lookup | query 或 assetId；libraryTier preferred/broad/all（默认all）、collection实际ID、requiresJoints true仅已核内部可动关节>0（false/省略不限）、kind model/material、category、textured、size；limit至多20；未知保持未知 |
| assets_prepare | assetId/url/sourceFile/resourceFile四选一、requestId；sourceFile需expectedSourceHash，可显式materialIds；resourceFile配显式dependencyFiles；在线需sourcePage/name，许可声明保留；异步准备 |
| scene_build | expectedSourceHash、seed、requestId、resourceRefs；environmentSourceRefs 显式选择完整环境源闭包（场景、任务、评分及实际依赖的helper、验证源与配置），省略兼容全工程冻结；baseRevision 只核基版存在，不提供增量编译 |
| scene_observe | revision、cameraId 或同版 cameraIds 批处理；或 nodeId 子树取景；回合用 episodeId/cameraId/frame；output=web_preview 单独重建显示预览，与相机/节点/回合参数互斥；原冻结源不改 |
| task_check | revision、taskIds、实际验证 episodeIds（推荐）或 episodeId；两者同时给时非空 episodeIds 优先，否则回退 episodeId；baseRevision 可返回全 World difference，不推导最小复验范围 |
| robot_run | revision、taskIds、behavior/controller、robotSetup 或 robotSetupRef、purpose、requestId；可选 rubricSeconds 为正有限的累计评分交换预算秒数，默认30，新冻结环境须有 adjustableRubricBudget=true；预算随原回合保存并用于同事实重算，不改变物理时长或评分含义；新安装须独立behavior；读每任务 score/实际I/O/安装来源/示范资格 |
| job | jobId、action status/wait/cancel、cursor、waitMs 至多 30000；远端确认取消才终态 |
| generation_wait_system | requestId、reason、非空 evidence 字符串列表、可选 jobIds；记录系统阻碍并等待原任务显式恢复，不取消既有作业或判断产物通过 |
| project_deliver | revision、environmentRefs、expectedRequirementVersion、requestHash、requestId、review 原生指针、evidence；新 producer-exports-v1 任务另给 producedArtifacts；独立内审与本次锁定 requiredOutputs 所请求的消费者通过后才 accepted |

`robot_run.controller` 为 `{"type":"expert"}` 或带明确 `policyRef/digest` 的 `{"type":"policy",...}`。policyRef 选择该任务 evaluation.cases 中的冻结验证源；同源有多个 case 时再给 `caseId` 消歧，不要求复制程序，摘要与 verification 用途仍须匹配。纯场景范围只允许该冻结验证源加 `purpose=verification`，运行回执为 `controllerKind='case'`；默认专家、显式专家、probe及独立 `behavior` 采集会在提交前拒绝。验证可驱动家具或以空命令推进物理，不要求机器人动作或任务成功。拟交付行为数据用 demonstration；用途不保证成功，demonstrationEligible 才是已核成功子集。probe/consumer_diagnostic 不满足正式行为项。任务列表分别重置，复合顺序由生成行为与评分表达；同版批采复用冻结 expert 和必要视角，当前 resetSpec 仅允许冻结 seed，同 seed 重复不是场景多样性。外部候选用独立 evaluate 入口，成绩不改变包的生成状态。

冻结案例的提交形状如下；`frozen_manifest` 取所选 revision 的真实已发布清单，`task_id/case_id` 从其中选择，requestId 使用本次稳定标识。不能从别版、当前未冻结文件或专家入口猜摘要。

```python
task = next(t for t in frozen_manifest['tasks'] if t['id'] == task_id)
case = next(c for c in task['evaluation']['cases'] if c['id'] == case_id)
run = robot_run(
    projectId=project_id, generationId=generation_id,
    revision=frozen_manifest['revision'], taskIds=[task_id],
    purpose='verification', requestId=run_request_id,
    controller={'type': 'policy', 'policyRef': case['sourceRef'],
                'caseId': case['id'],
                'digest': frozen_manifest['sourceDigests'][case['sourceRef']]},
)
```

先等该 job 终态，再取回执中的真实 episode ID，用同版 `task_check(episodeIds=[...])` 对照冻结期望；pending 时不猜 episode。

写工具第一次调用选稳定 requestId，网络重试保留完整原工具名与输入；有意修复后新试验用新 requestId。`changedNodeIds` 仅在 Schema 中保留 optional deprecated 历史兼容声明，不参与影响分析；同作用域历史请求精确匹配时原样返回旧回执，无历史的新请求带此字段明确拒绝。长操作返回 jobId，有界 wait 的 pending 不是失败；终态含实际结果，观察图片不能以纯 URL 文字代替。

在线 URL 准备在持久化 job 后后台下载/暂存并立即返回，HTTP 断开或 MCP 等待超时不取消已受理作业，显式 job cancel 或服务关闭才驱动本地准备收尾。关闭只在本地清理窗口内有界等待，资源退出未确认保留诊断，不覆盖首错。取消处于 online_stage 且尚未提交计算的准备，只确认不会再提交计算，不代表暂存 SSH 已退出；remote_submit 后按原 job 请求远端取消，确认前不判终态。重启时无 payload 的中断准备明确失败；有 payload 只证明提交意图，按原 jobId 对账，不盲目重下或重提。历史失败沿原 ID 返回，新尝试用新 ID。

运行器 CPU 操作与 GPU 执行分开：health/assets/inspect/job/check/read/list/write 及 submit(task.operation=check) 不因缺当前 GPU 配置阻断，并忽略用户伪造的设备字段；prepare/observe 等其他入口仍保守要求已配置设备，真实执行继续锁定 index+UUID。这些是运行器 operation 名，不是额外 MCP 工具，也不代表所有含 CPU 阶段的通路均无设备依赖。

交付 evidence 只表达自然语言需求与实际 artifactRef 的对应，不能传 `passed:true` 覆盖宿主检查。未满足全局原文、树未展开、缺本次范围所需的真实记录或消费者时保留 incomplete。

### 等待系统处理

只有确认被工程之外的系统工具、执行器或回执故障阻挡时才报告；具体产物失败继续由生成器按本次范围自主修复。当前协议工程runtimeCompatible=false是正常版本门，场景任务可按当前接口在原工程重建和验证，只采集任务保留环境兼容缺项；保留旧包，不能只凭旧回执或物理重演差异宣布当前系统仍故障。调用 `generation_wait_system(requestId, reason, evidence, jobIds=...)`，公共 projectId/generationId 与当前工程一致。evidence 为实际错误、已保存日志或作业回执位置的非空字符串列表；jobIds 如果提供，必须属于当前生成任务。它记录的是阻碍声明，不认证根因，也不替代内部质量审核。

收到 waiting_system 后结束当前轮次。当前工具调用、已受理作业和原生子任务正常完成或对账，仍可 inspect、查询和取消原作业；新的计算会被拒绝。自动轮询、子任务完成、桥重连及普通消息不解除等待。传输重试保持原 requestId；同标识改输入会被拒绝。修复后若发生新的阻碍，使用新的 requestId，旧请求的迟到重试不能再次锁住已恢复的任务。

宿主对自动续接检查可观测的持久推进：源码/机器人配置、需求、实际作业以及真实原生协作完成指针。只查询既有状态或重复解释不计推进；活动或状态未明的原生子任务先对账。无可观测推进只是等待处理原因，不是系统故障或产物质量判决。

修复后由显式 `POST /api/projects/:projectId/resume` 提交同一 generationId；网页提供“处理后继续”。宿主确认原生空闲、无未决作业、审批或不确定发送后再恢复，同要求、原生会话及失败记录保留。等待期间普通补充要求可保存，不能启动或 steer 原生轮次。系统等待与审批等待不重复计时；不限总时长仍为 timeBudgetMs=null，单次执行进程的超时保护独立存在。

## 内部独立审核

生成器自主召集原生直接子代理 `scene_quality_review`，由其独立检查完整要求和当前产物，不兼任作者、不编辑产物。审核者自行读取原文、冻结 World、评分源码与适用验证案例、实际图像和拟交付执行结果，涉及机器人时核型号、模型来源和运动结构是否保留，不能只依赖作者总结。按实际任务选择观察范围和关键时刻；已有同版图像与记录可复用，不固定各相机首中末。发现问题由生成器按本次范围自主修正和复审，场景变化才重建环境；未满足或无法验证的要求照实保留。具体审核内容与最终质量判断属于生成器内部职责。

作者与审核者按[场景自检与内审](../SKILL.md#场景自检与内审)的五方面检查当前请求的完整场景，包括未点名的可见背景家具；实际图像同时覆盖整体与必要操作细节。房间类型可辨、任务评分或技术回执通过不能替代这些判断。明确属于不同风格、正常装配或不适用的条件按原文解释，不把论文指标的数值或统一机器人尺寸硬套到所有任务。

几何核验从当前冻结stage的实际初始位姿出发。可用锁定SDK的 `UsdGeom.XformCache` 读取世界变换、`UsdGeom.BBoxCache` 定位实际可见几何的范围，再核支撑面、接触或所需几何查询；默认/渲染几何与guide碰撞代理分开。包围盒只筛候选，不能单凭重叠宣称实体互穿或容纳成立。非封闭网格、薄壳和软体不套未经核验的实体包含算法；视觉网格与碰撞体的错误也分别说明。必要时在工程内编写只读量测或验证源，记录其输入版本与实际事实，不增加内核家具配方。

支撑按整物体的真实承托/安装路径核验：床腿可以承托床架，吊柜可固定在墙上，嵌装水槽可由台面承托；既不要求每个部件落地，也不能仅凭所属组或supported_by豁免错误。地面、桌面、房间内部与墙/顶安装面分别作为适用边界，不能混用；任务操作位置应在工作区可用的一侧，末端接近和部件扫掠另查，不能把吊柜或水槽下方当作必需站位。

物理核验保留正确固定、连接、关节和材料类型，结合现有实际记录或适用验证案例检查异常坍塌、穿落、任务所需活动响应、碰撞形状及可读状态。检查对象是否有真实可用的容腔与活动空间，不只数刚体/关节或看是否静止；不能全部释放安装件或全部固定可移动物体来取得稳定。液体/软体按任务需要核实际状态与表现，不强制所有任务采用同一种时长、阈值或机器人成功演示。未知、近似误差及未观察范围照实保留，不从数值步进成功推导完整物理正确。

在既有requirements、views、issues或说明中交代五方面的实际依据、问题及必要未核项；没有充分依据的必要要求不作通过。作者自主修正后由同一内部流程复审当前版本，未变更的同版事实可以复用。宿主不解释这些质量说明、不检查固定工具步骤，也不因增加此指导而引入新的必填回执字段。

审核者在其最新已完成轮次的最后一条 agentMessage 返回单一完整 JSON，schema 为 `sceneagent-internal-review-v2`；phase 为 final_answer，协议 phase=null 时兼容完成轮次最后消息。交付读取以下字段：

- `projectId/generationId/requirementVersion/requestHash/sourceHash/revision`：当前工程、生成任务、完整原文及冻结源版本的绑定。
- 当前范围合同在回执顶层原样返回 `requiredOutputs/environmentRefs`，与本次申请和已锁定选择一致，含所选 revision 及适用的 taskIds/split；不能漏填、套用默认三项或重新选范围。请求 evaluation 时，每个 revision 须保留其完整冻结任务集，taskIds 可省略或显式全量列出，不能缩减。旧合同沿其冻结定义，不混填新字段。
- 新 `producer-exports-v1` 契约另在回执顶层返回 `producedArtifacts={manifestRef,sha256}`，与作者申请交付的实际清单相同。导出内容或清单改变后由内部审核者复审新摘要；旧任务无此字段时不追加。
- `decision`：pass 或 needs_changes；只有 pass 可申请交付。
- `coverage.episodes`：未请求 demonstrations 时为 `[]`，适用验证案例仍由任务检查与评测产物记录，不混入示范选择；请求 demonstrations 时为非空、无重复的 `{episodeId,revision,use}` 数组，各 revision 与 environmentRefs 匹配，不同时传旧 episodeIds。每个必需的版本/任务至少一份当前同代真实执行记录；use 为 `execution_data` 或 `successful_demonstration`。前者不宣称成功，后者要求实际完整执行、result.success=true 且 demonstrationEligible=true。本次交付范围要求完成机器人任务或成功示范时仍需真实成功证据，不能只选失败数据宣布完成。旧合同条目沿其冻结字段定义。

审核者可附 requirements、views、rubrics、issues、resolvedIssues、证据引用和说明，用于自身修正与追溯；宿主不解释这些工作字段的质量含义，不检查逐项工具使用、图像输入、固定帧或历史问题关闭关系。它们不是第二套系统评分或审批。

作者调用 `project_deliver` 提供 `review={threadId,turnId,itemId}`，指向该直接子审核者的最新完成轮次最终 pass。宿主只核原生身份与直接父会话/角色、最新轮次完成、最终指针、当前绑定和固定执行集合；不需要完整 MCP 看图完成通知。普通同目录会话、作者自报文件或旧 pass 不能代替当前原生回执。

执行集合从当前同代同版已完成 rollout 作业匹配，技术消费者按本次范围核发布清单、适用的冻结行为/验证与评分源、真实记录与时钟、数组与媒体、同事实评分及成功用途资格。读取审核回执本身不表示本次请求产物已通过。源码、需求、冻结版本或拟交付集合/用途变化须复审；同版新记录不自动混入旧申请，新的有意交付尝试使用新 requestId，历史失败保留。准备、提交、终态只重新读取所选最新原生回执，保存既有 `.trace/inputs` 小回执和摘要；连接或读取失败照实返回，不回退缓存通过，也不为此复原全部审核历史。只有本次请求的消费者通过并持久保存 accepted 才能宣布完成。
