# 真实机器人候选与资源获取

适用于生成器自主选择机器人、接入新型号，以及内部审核检查机器人来源。候选用于发现资源，不是系统型号白名单；不在清单中的真实常用型号也可选择。正式机器人必须有可核型号和模型出处，不能按任务自造滑轨、连杆或关节机构，也不能给真实型号加自由度、缩改连杆或换结构以取得成功。这一要求覆盖配置、直接 `world.stage` 构建和生成资产的所有路径。

允许选择基准或厂商已公开的组合构型，并准确披露底盘、手臂、夹具和版本；也可分别加载多台真实机械臂实例共同执行，保持每台原机构。生成器编写的是接入配置、控制程序与任务，不是新的机器人设计。配置缺口不能用简化机器人替身补齐。既有官方模型的仿真近似也须说明，不能称完全等同真实硬件。

## 选择与使用边界

用户指定机器人时按指定型号；未指定时按可达范围、负载、夹具、任务和可用模型自主选择。单臂、双臂、移动操作与人形分别匹配实际控制能力；不默认每个任务都用同一型号。

模型、关节和控制来源需一致，Panda、FR3、UR5e、UR10e以及不同厂商同名G1不能互称。保留原机构和米制尺寸；可做资源路径修复、命名与坐标映射、受支持材质转换及官方导入。记录原始和派生文件摘要及转换范围，未知物理差异保留缺项。固定基座或仅控制部分关节应符合实际任务设置并披露，不能以固定人形底座替代任务所需的行走或平衡。外部控制示例中的状态传送、辅助抓握或物体附着不自动获得正式机器人动作资格。

下表只提供型号与来源线索，核对日期为2026-10-21。基准里的模型文件、动作约定和策略不自动跨仿真器兼容；下载前核具体版本、许可和完整依赖，使用前核实际模型。

## 候选来源

| 来源 | 可考虑的真实型号或既有构型 | 来源与接入注意 |
| --- | --- | --- |
| LIBERO | Franka Panda及Panda夹爪 | [基准机器人定义](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/master/libero/libero/envs/robots/on_the_ground_panda.py)、[robosuite 1.4 Panda模型](https://github.com/ARISE-Initiative/robosuite/blob/v1.4.0/robosuite/models/assets/robots/panda/robot.xml)；Panda不等于FR3。基准MJCF不是本系统配置的直接模型入口。 |
| RoboCasa | PandaOmron：Panda臂、升降躯干与Omron移动底盘的既有组合 | [默认环境设置](https://github.com/robocasa/robocasa/blob/main/robocasa/utils/env_utils.py)、[组合模型](https://github.com/ARISE-Initiative/robosuite/blob/master/robosuite/models/robots/compositional.py)；应保留移动底盘与夹具说明，不能把其轨迹称作固定Panda基准结果。 |
| RoboTwin | 默认Aloha-AgileX双臂；Piper、ARX-X5、Panda、UR5+WSG等按各自独立配置 | [默认任务](https://github.com/RoboTwin-Platform/RoboTwin/blob/main/env_cfg/task_config/demo_clean.yml)、[型号映射](https://github.com/RoboTwin-Platform/RoboTwin/blob/main/env_cfg/task_config/_embodiment_config.yml)、[官方模型包](https://huggingface.co/datasets/TianxingChen/RoboTwin2.0/tree/main)。锁定分支并按配置解析左右臂与夹具；Aloha-AgileX不能改名为Piper。 |
| BEHAVIOR / OmniGibson | 2026挑战默认Galaxea R1Pro；另有Fetch、TIAGo等 | [机器人列表](https://github.com/StanfordVL/BEHAVIOR-1K/blob/main/docs/omnigibson/robots.md)、[挑战默认R1Pro配置](https://github.com/StanfordVL/BEHAVIOR-1K/blob/main/OmniGibson/omnigibson/eval/r1pro.yaml)。不选仅作VR代理的`BehaviorRobot`来替代真实机器人；基准辅助抓握设置不自动继承为真实接触抓握。 |
| RoboDojo | 双ARX X5；其他构型按官方配置核实 | [官方双X5配置](https://github.com/RoboDojo-Benchmark/RoboDojo/blob/main/env_cfg/robot/dual_x5.yml)、[USD模型加载配置](https://github.com/RoboDojo-Benchmark/RoboDojo/blob/main/env/robot_manager/robot_config/x5.py)、[官方资产包](https://huggingface.co/datasets/RoboDojo-Benchmark/RoboDojo/tree/main)。默认双臂每臂6关节；竞争任务另加Panda支援臂的配置须单独说明，真机Piper配置不冒充仿真默认。 |
| 宇树 Unitree | G1、H1等人形；Z1机械臂 | [G1官方描述及变体表](https://github.com/unitreerobotics/unitree_ros/tree/master/robots/g1_description)、[H1描述](https://github.com/unitreerobotics/unitree_ros/tree/master/robots/h1_description)、[H1-2独立目录](https://github.com/unitreerobotics/unitree_ros/tree/master/robots/h1_2_description)、[官方仿真与策略入口](https://github.com/unitreerobotics/unitree_mujoco)。G1的23/29自由度及手型、H1与H1-2分别核对，优先选匹配变体的URDF/USD；有模型不等于已有可用平衡、行走或操作策略。 |
| 智元 AGIBOT | Genie G1、Genie G2及其已公开臂/夹具组合 | [G1双臂配置](https://github.com/AgibotTech/genie_sim/blob/main/source/data_collection/config/robot_cfg/G1_omnipicker_fixed_dual.json)、[G2双臂配置](https://github.com/AgibotTech/genie_sim/blob/main/source/data_collection/config/robot_cfg/G2_omnipicker_fixed_dual.json)、[官方USD/网格资产](https://huggingface.co/datasets/agibot-world/GenieSimAssets)；核具体URDF/USD、网格及策略依赖。这里固定双臂/锁躯干规划变体不代表完整移动全身模型；资产页当前许可为CC BY-NC-SA 4.0，使用时核所选文件许可。不与Unitree G1混用，也不以厂商名代替型号。 |
| 其他常用机械臂 | UR5e、UR10e等 | 厂商或锁定SDK中可核来源的模型与规划配置；继续使用相同配置入口，不增加型号分支。两台UR10e是组合配置，不是独立市售型号。 |

## 本地资源与外部获取

先查当前宿主的已准备资产及锁定 SDK 前缀。本地存在、准备资格和实际 I/O 以当前 project_inspect、模型和冻结报告核对，不把来源表维护为另一套 ready 状态。历史型号的加载、驱动或策略验证不授予本次任务成功资格。

2026-10-23 已准备资源的位置、原始入口、版本和已知文件缺项，见当前配置数据根的 `assets/robots/public-20261023/resource-inventory.json`；该清单只记录下载和静态依赖核对。优先复用这些原始文件，已有缓存保持只读，派生文件仍放当前工程的 imports。清单中的 LIBERO/PandaOmron 原始 MJCF 不是可直接加载的 URDF/USD，SDK 材质模块也不因文件存在就代表已通过渲染或机器人执行。

BEHAVIOR 的独立未加密 `omnigibson-robot-assets` 包与加密场景物体包分别核许可；前者[官方声明 MIT](https://huggingface.co/datasets/behavior-1k/omnigibson-robot-assets)，不能把后者的使用范围笼统套到所有机器人资源上。

本地缺少合适模型时，用原生搜索与终端从厂商或基准官方发布取得完整包，固定release/commit及具体构型。资源写入当前宿主配置数据根的 `assets/imports/<projectId>/` 独立新目录，机器人也可用 `assets/imports/robots/<projectId>/`；保持原相对布局和已有库只读。Git LFS网格、纹理、碰撞文件及URDF的包引用都需取得真实文件。多个原始来源分属不同根时，先在独立新目录整理完整闭包，再冻结，不放宽到任意主机目录。

原生模型按[原生文件包接口](api.md#资产与材料)准备完整闭包，出处复用报告的 name/sourcePage/license；再把报告引用传给 scene_build。自填名称、来源页或许可声明不能认证模型真实性、使用许可或任务成功，生成器及内部审核者仍核实际来源和结构。

当前`assets_prepare(url=...)`只收自包含HTTPS GLB，不能用于下载URDF、USD或ZIP机器人包。GLB主要是视觉几何，不因有机器人外观就具备真实关节和驱动。`world.robot`直接加载USD/USDA/USDC或URDF；MJCF/XML、xacro、压缩包和模型网页都不是可直接加载的入口。只有其他格式时，优先取得同型号官方USD/URDF；使用现有受支持的官方转换途径时保留原输入、完整依赖、转换源和差异记录，再按[机器人配置](api.md#机器人配置)核对结构与实际I/O，不改后缀冒充兼容。

新型号由生成器新增公开 `*.robot.json`，复用同一加载、规划、执行与记录链；字段和实际 I/O 见[机器人配置](api.md#机器人配置)。模型可加载、短段驱动、规划、抓取、协同、行走与完整任务成功分别说明。策略不足时继续开发、查匹配算法或保留缺项，不能换成自造机构；确认工程外系统故障阻挡时再用[等待接口](api.md#等待系统处理)，不把一般策略失败转交系统开发代理。

系统仍核资源和实际 I/O，机器人是否符合选型要求由生成器内部审核，不新增宿主型号审批或品牌认证。
