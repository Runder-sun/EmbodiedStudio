# EmbodiedStudio

[English](README.md)

EmbodiedStudio 是一个在浏览器里制作机器人仿真任务的工作台。描述你想做的任务，Studio 会用你自己的 Codex CLI 编写场景和评分程序。你可以查看三维场景，回放机器人在仿真中的执行录像，并查看这次执行的得分。

## 启动工作台

安装 [Node.js](https://nodejs.org/) 22 或更新版本，在仓库目录运行下面两条命令。

```sh
npm ci
npm start
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。首次启动会打开「工作台设置」。工具栏可以切换中英文，也可以点「设置」重新打开向导。

只打开工作台，有 Node.js 就够了。要生成场景，先在运行 Studio 的电脑上登录自己的 [Codex CLI](https://developers.openai.com/codex/cli/)，再选择仿真运行的位置。

| 你的设备 | 仿真在哪里跑 |
| --- | --- |
| 一台有 NVIDIA RTX 显卡的 x86-64 Linux 电脑 | 就在这台电脑上跑。设置里选「工作台主机」。 |
| Mac 或另一台工作电脑 | 在自己的 x86-64 Linux RTX 服务器上跑。设置里选「SSH 计算服务器」。 |

已经装了 Isaac Sim，就在「设置」里填它的 Python 路径。还没装的话，先保存草稿，再从安装项里选「Isaac Sim 环境」。环境准备好后，检测计算主机、选好显卡、保存并启用草稿，再重启 Studio。当前使用 Isaac Sim `6.0.0.1` 和 Python 3.12。 [部署详解（英文）](docs/compute.md)

## 先试一个桌面任务

新建工程，在输入框里试试这句。

> 做一个桌面工作区，放一个红色方块和一个浅托盘。任务是把方块放进托盘。

首次可以选「场景与评测环境」，构建后打开三维视图和场景树。想让机器人执行并采集数据时，选「完整生成」。你可以提供机器人模型包，也可以告诉 Codex 要找哪个型号。机器人还需要能完成这项任务的控制程序或策略。

源码包包含工作台和仿真运行器。场景资产可以用公开 GLB 链接，也可以接入自己的 USD、URDF 文件或已有资产库。 [资产接入（英文）](docs/assets.md)

## 生成结果怎么用

| 产出 | 可以拿来做什么 |
| --- | --- |
| 场景构建记录 | 查一次场景改动对应的源文件修改和构建版本。 |
| 机器人执行数据 | 读取仿真回合里保存的观测、动作、相机画面和评分。 |
| 仿真评测环境 | 换一个兼容的策略，在同一冻结场景里用同一套评分程序评测。 |

程序接口和产出文件格式见[生成接口](skills/scene-generation/references/api.md)和[交付说明](skills/scene-generation/references/delivery.md)。

## 开发与测试

`npm test` 会运行工作台和工具接口的测试。

<details>
<summary>Python 测试</summary>

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m tests.run_suite tests.test_runtime tests.test_planned_motion tests.test_records
```

如需跑全套测试，请在装好测试依赖的 Linux Isaac Sim 环境中运行 `python -m tests.run_suite`，并使用 SDK 自带的 OpenUSD 和 PhysX 插件。渲染与机器人执行需要在计算主机上单独验证。

</details>

## 许可证

参见[源码许可证](LICENSE)和[第三方组件条款](THIRD_PARTY.md)。资产、模型和数据集按各自来源的许可使用。
