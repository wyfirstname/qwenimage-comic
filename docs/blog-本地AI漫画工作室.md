# 我在 6GB 显存的游戏本上，做了一个完全离线的 AI 漫画工作室

> 一段剧本进去，一本带气泡、能导出 PDF 的漫画出来。不联网、不调 API、数据不出本机。

---

## 起因

事情的开始很简单：我想让自己写的小故事变成漫画，但又不想把东西交给任何在线服务。

市面上的方案基本是两条路：要么用在线 AI 绘画平台（提示词、参考图、生成结果全在别人服务器上），要么自己在本地拼工具链——但本地方案通常停留在"能出一张图"，从图到"一本漫画"中间的活全得手工干：角色每格长得都不一样、场景对不上、对白要自己在 PS 里往上加、排版一页页手动拼。

我真正想要的是一个**流水线**：给一段剧本，最后拿到一本 PDF。

于是就有了这个项目：**QwenImage Comic**，一个跑在自己电脑上的 AI 漫画工作室。

先看成品。下面这 8 页《水漫金山寺》，从剧本、角色立绘、场景概念图、分镜图到排版成页，**全部由本地模型生成**，没有一步经过网络：

| 第 1 页 | 第 2 页 |
|:---:|:---:|
| ![第1页](https://cdn.jsdelivr.net/gh/wyfirstname/qwenimage-comic@main/docs/preview/page-1.jpg) | ![第2页](https://cdn.jsdelivr.net/gh/wyfirstname/qwenimage-comic@main/docs/preview/page-2.jpg) |

| 第 3 页 | 第 6 页 |
|:---:|:---:|
| ![第3页](https://cdn.jsdelivr.net/gh/wyfirstname/qwenimage-comic@main/docs/preview/page-3.jpg) | ![第6页](https://cdn.jsdelivr.net/gh/wyfirstname/qwenimage-comic@main/docs/preview/page-6.jpg) |

> 更多页面和完整界面截图见 [GitHub 仓库](https://github.com/wyfirstname/qwenimage-comic)。

---

## 它到底做了什么

核心是**六步工作流**，每一步的"智能"部分都由本地模型完成：

| 步骤 | 做什么 | 谁干的 |
|---|---|---|
| ① 剧本 | 粘贴手写剧本，或给个梗概让 AI 写 / 润色（超 4 页自动分幕续写） | Qwen3-VL-8B |
| ② 角色 | 从剧本里提取人物 → 按人设自绘立绘，固定种子保证一致性 | Qwen3-VL-8B + Qwen-Image 2.1 |
| ③ 场景 | 提取场景设定卡 → 生成场景概念图 | Qwen3-VL-8B + Qwen-Image 2.1 |
| ④ 分镜 | 智能拆格，自动绑定角色与场景，AI 失败时有规则兜底 | Qwen3-VL-8B |
| ⑤ 出图 | 逐格生成，**角色立绘 + 场景概念图一起喂给模型融合** | Qwen-Image 2.1 |
| ⑥ 排版 | 拼页 + 自动气泡 / 拟声词 / 页码 → 一键导出整部 PDF | PIL |

剧本格式刻意做得很好写，一行就是一格：

```
第 1 页
【远景】雨夜的旧城区，霓虹在积水里碎成一片。
[特写] 小雨的眼睛。「这就是最后一战了吗？」
林默：别回头，跟我走。
[音效：轰隆] 雷声滚过楼群。
== 第2页 ==
[动作] 两人在巷道里狂奔。
```

`[景别]` 指定镜头、`「对白」`/`角色名：台词` 识别成气泡、`[音效：xx]` 生成拟声词角标。不写景别就按节奏自动轮换。

除了漫画工作室，主界面还有独立的**文生图 / 图生图**：提示词、负面词、尺寸预设、步数、CFG、种子锁定、批量张数，图生图支持拖拽参考图和重绘强度，任务队列走 SSE 实时推进度，图库能搜索、收藏、按参数复现。

---

## 技术栈

```
前端    原生 HTML / CSS / JS（无构建步骤，改完刷新就见效）
后端    FastAPI + SQLite
出图    Qwen-Image-2.1-Uncensored-GGUF，经 ComfyUI + ComfyUI-GGUF 加载
文本    Qwen3-VL-8B（复用出图模型自带的文本编码器）
排版    Pillow（气泡、拟声词、页码、拼页、合成 PDF）
```

规模：后端约 **8700 行 Python**，前端约 **3500 行**，漫画模块 **49 个接口**，另有 **5 个回归测试文件**覆盖融合通道、PDF 导出、跨机器数据迁移等易错路径。

硬件门槛低到有点意外：**RTX 2060 6GB**（我自己的游戏本）实测可跑，代价是慢——768×768 / 8 步约 **18 分钟一张**。

---

## 六个真正花时间的坑

工具链搭起来不难，难的是让它真的跑通。下面这些是项目里最有价值的部分，也是我在别的地方没找到答案、靠自己一点点啃出来的。

### 坑 1：Qwen-Image 2.1 根本进不了 diffusers

最开始我走的是标准路线：`diffusers` + `AutoPipelineForText2Image`。结果直接加载失败。

原因：Qwen-Image 2.1 是新架构（`qwen_image21`），而 diffusers——**包括当时的最新版**——只支持到上一代 `qwen_image`。我在 diffusers 源码里翻了一遍，确实没有对应的 pipeline 实现。

把能试的都试了之后，结论是：**Python 生态里目前唯一可用的通道是 ComfyUI**。

ComfyUI 0.37+ 已经原生支持这个架构，配上 leejet 的 **ComfyUI-GGUF** 插件就能直接加载 GGUF 量化权重。于是整个推理层被换成了 ComfyUI：后端在启动时把 ComfyUI 作为子进程拉起来，通过 HTTP + WebSocket 与它交互。

这一步的附带发现：**进度必须走 WebSocket**。ComfyUI 在采样期间 HTTP 请求会被阻塞，靠轮询 HTTP 拿进度是拿不到的，只能挂 WebSocket 收 `progress` 消息。

### 坑 2：6GB 显存怎么塞下 9.35GB 的文本编码器

模型总共有 14.6GB，拆开看是这样的：

```
qwen-image-2.1-UC-Q4_K_M.gguf            4.60 GB   扩散模型
qwen3vl_8b_int8_convrot.safetensors      9.35 GB   文本编码器
qwen_image_2.1_vae_bf16.safetensors       0.68 GB   VAE
```

问题很明显：**光文本编码器就 9.35GB，比我的显存还大 50%**。

解法是把文本编码器整体放到 CPU 上（`.env` 里的 `TEXT_ENCODER_DEVICE=cpu`）。文本编码只在每次采样前跑一遍，放到 CPU 上损失的时间可以接受，但**直接省下 9~17GB 显存**。剩下的 4.6GB 扩散模型 + VAE 才放得进 6GB 显存。

配合 `--lowvram` 启动参数和 `VAE_TILTING`（大图分块解码防 OOM），6GB 能跑通。

代价就是慢：Q4_K_M 在这个配置下约 **152 秒/步**，8 步就是 20 分钟上下。所以我把单张推理超时设成了 2400 秒——默认的几十秒会直接把任务掐死。

> 顺带一个反直觉的结论：**512×512 / 4 步出的图基本不能看**，768×768 / 8 步才是实用下限。在小显存上"用小图快速试构图，满意了再重绘"是唯一效率可接受的打法。

### 坑 3：角色一致性——拼贴是死路，得用官方多参考图通道

这是整个项目最核心的技术点，我走了两条死路才找对。

**死路一：低去噪强度 img2img。** 思路是拿"角色立绘 + 场景图"拼成一张参考图，然后低重绘强度跑 img2img。结果很残忍——**低去噪时构图完全由参考图主导，物理上不可能"融合"**。立绘拼贴上去，出来还是只有人物；场景图铺满，出来只有背景。这条路根本不通，后来我把拼贴函数直接删了。

**死路二：干脆纯文生图。** 角色长相每次都不一样，一致性全丢。

**正确的解法是 Qwen-Image 2.1 官方的多参考图编辑通道**（ComfyUI 里的 `TextEncodeQwenImage21` 节点）：

- **image_1** = 场景概念图，`cover_fit` 铺满目标尺寸。注意——这里它同时决定了输出尺寸
- **image_2…N** = 各个角色的立绘，长边限制 512px
- latent 用节点输出的**空 latent**，而不是从参考图 latent 出发
- 关键参数：**CFG=1、denoise=1、euler/simple**。CFG=1 意味着**负面提示词不生效**，别再写反提示词了
- 提示词里用 `image_1` / `image_2` 指代参考图，并明确要求"自然融入场景、禁止拼贴/禁止并排"

这样模型是"边看参考图边重画"，而不是"照着参考图描"。两三个角色融进雨夜天台这类场景，实测是成立的。

这里还有两个必须记住的细节：

1. **传了 `vae` 之后，参考图走的是 VAE latent 拼接**，不是视觉塔 token 路径（`keep_vision=False`），别指望模型"理解"参考图语义，它是按 latent 对齐的。
2. **角色形象图要用半身立绘，不要全身设定图。** 脸的像素占比直接决定 img2img 能不能锁住长相——全身图里脸太小，锁不住。

还有一个坑值得单独拎出来：**当没有任何可用立绘时，绝对不要退化成"纯场景融合"**。模型收到"保持 image_1 构图"的指令却没有人物参考，会原样复刻一张空背景给你。这种情况必须老老实实退成文生图。我在另一台机器上复现过这个问题，最后是靠日志里那行 `[comic] 分镜#N 景别 → 2.1融合：X张参考图 / 文生图（原因）` 才定位清楚的——**给降级路径留一行日志，比什么都值**。

### 坑 4：不下载语言模型，蹭出图模型的文本编码器

漫画工作流里到处需要文本能力：解析剧本、提取角色、提取场景、智能分镜、写剧本、润色。

标准做法是再下一个语言模型，但 14.6GB 的模型已经够占地方了。我注意到一件事：**Qwen-Image 2.1 的文本编码器本身就是 Qwen3-VL-8B**——一个完整的 8B 模型。

那为什么不用它？

ComfyUI 里用 `CLIPLoader(type=qwen_image)` 把编码器加载进来，接一个 `TextGenerate` 节点，就能直接做文本生成。于是**零额外模型体积**拿到了一个能用的 8B 文本模型。

踩的坑也很典型：

- 图节点不挂输出节点（`SaveText`）会直接报 `prompt_no_outputs`；
- device 必须填 `"default"`，填 `cpu` 会报 `Expected a cuda device`（这个报错信息挺误导人的）；
- 采样参数要用点号扁平键 `sampling_mode.temperature`；
- 结构化提取（比如"把剧本解析成 JSON"）用 `sampling_mode="off"` 贪心解码最稳。

速度约 **1.8 秒/字**（CPU 推理），写一整套剧本要十几分钟。所以所有 AI 文本步骤都做成了**单并发后台任务**，前端轮询状态、可随时取消，不阻塞界面。

### 坑 5：绝对不要把拼装结果回写进拼装的输入字段

这个是"设计错误"，不是"技术难点"，但造成的破坏最大，值得单说。

场景概念图这一块，我最初的设计是：AI 提取出场景提示词 → 存进 `prompt` 字段 → 生成时读取 `prompt` 拼装成完整提示词 → **把完整提示词也写回 `prompt`**。

看起来很自然对吧？结果就是**每生成一次就套一层包装**：画风描述、`background concept art`、`location setting` 各多一份。生成三次之后提示词长到 1045 字，出图必坏。

修复方式是把"输入"和"输出"彻底分开：

- `prompt` = AI 提取的核心提示词，**唯一输入**，前端可编辑
- `last_prompt` = 实际使用的完整提示词，**只用来展示，不参与任何拼装**

这条教训我想扩大到所有类似场景：**只要一个字段既是拼装的输入又是拼装的输出，它一定会套娃。** 现在就写死一条规矩——拼装结果只允许写进"只读展示"字段。

### 坑 6：别把 MB 级 base64 塞进 SQLite

这是最惊险的一次事故。

分镜融合任务需要把参考图传给引擎。当时图省事，直接把参考图的 base64 塞进了任务参数，存进 SQLite 的 `jobs.params_json`。单个任务记录 **4~5MB**。

跑了一段时间后，漫画接口开始集体报错：

```
database disk image is malformed
```

数据库坏了。

排查过程记一下，因为它救了我一次：用 `?mode=ro&immutable=1` 打开数据库（这会**跳过 WAL 文件直接读主库**），结果**能正常读、`integrity_check` 返回 ok**。这说明**主库是好的，坏的是 `app.db-wal`**。

把 `-wal` / `-shm` 移出目录（先备份三件套），数据库立刻恢复正常，数据完好。

根因修复分两步：

**第一步，大字段外置。** 新增 `blobs.py`：任何超过 4KB 的字符串参数，写到 `data/blobs/<job_id>/<n>` 文件里，数据库里只留一个 `@blob:<job_id>/<n>` 占位符。任务执行时展开，结束后释放，启动时清理孤儿。接线点只有两处——创建任务时 pack、执行任务时 unpack。

**第二步，给历史库瘦身。** 启动迁移把已结束任务里的超长参数换成说明文本，然后 `VACUUM`。

这里有个细节差点让我白干：**WAL 模式下只 `VACUUM` 是不够的**，主库文件不会变小。必须在 `VACUUM` **之后**再执行 `PRAGMA wal_checkpoint(TRUNCATE)`。

结果：**156.8MB → 1.37MB**。

---

## 几个顺手做掉的工程细节

**数据全部落项目目录。** 数据库、出图、导出 PDF 都在 `data/` 下，删掉 `data/` 就是完全重置。素材路径统一存**相对路径**（`data/...`），不存绝对路径——这样整个项目目录可以整体拷到另一台电脑继续用。

**跨机器拷库的兜底 bug。** 有一次从另一台电脑拷了数据库过来，导出 PDF 报"成品页文件缺失"。原因是那条兜底逻辑写成了：

```python
path = _resolve_stored_path(record["path"]) or _resolve_stored_path(record["url"])
```

`_resolve_stored_path` 返回的是一个 Path 对象——**即使文件不存在，对象本身也是"真值"**，导致 `or` 后面的 URL 兜底永远轮不到。修复是改成"按候选顺序返回第一个**真实存在**的文件"。

教训记住：**`A or B` 兜底必须验证 A 真的可用；能构造出对象 ≠ 文件存在。**

**出图按项目归档。** 漫画的图落 `outputs/comics/<项目名>_<id尾6位>/`，项目改名图片不搬家，同名项目靠 id 后缀区分。启动时自动把历史遗留的散落文件归位（幂等、同名不覆盖）。

**前端缓存。** StaticFiles 挂载的 JS/CSS 默认不带 `Cache-Control`，浏览器启发式缓存会让"改了前端没生效"。现在 `/static` 强制 no-cache，`index.html` 里的资源引用带 `?v=<mtime>` 指纹。

**PDF 导出防重复。** 每次排版都会新增一批成品页，所以导出时**每个页码只取最新一份**，按页码升序合并。DPI 按页宽/210mm 反推（1240px → 150dpi ≈ A4）。黑白版在合成时转灰度，文件名带 `_黑白版`。

---

## 怎么装

> 完整步骤在仓库 README，这里给最短路径。

**先试水（不需要显卡、不装模型、不装 torch）：**

```bash
git clone https://github.com/wyfirstname/qwenimage-comic.git
cd qwenimage-comic
# Windows 双击 start.bat；Linux/macOS：bash start.sh
```

脚本会自动建虚拟环境、装 Web 依赖、生成 `.env`、起服务。打开 http://127.0.0.1:8000 ，右上角显示「占位模式」就是跑通了——界面、队列、图库、漫画全流程都能点，只是出图是占位图。

**要真实出图，再补三步：**

```bash
# 1. 推理依赖（约 3GB，含 torch）
.venv\Scripts\pip install -r backend\requirements-inference.txt

# 2. clone ComfyUI + GGUF 插件，并让它复用本项目的 models/ 目录
git clone --depth 1 https://github.com/comfyanonymous/ComfyUI comfyui
.venv\Scripts\pip install -r comfyui\requirements.txt
git clone --depth 1 https://github.com/leejet/ComfyUI-GGUF comfyui/custom_nodes/ComfyUI-GGUF
copy scripts\extra_model_paths.yaml comfyui\extra_model_paths.yaml

# 3. 下载模型（14.6GB，走 hf-mirror 国内镜像，支持断点续传）
download_models.bat
```

然后改 `.env`：

```ini
ENGINE_MODE=comfy
TEXT_ENCODER_DEVICE=cpu     # 6GB 显存必开
INFER_TIMEOUT_SECONDS=2400  # 小显存出图慢，超时要留够
```

重启 `start.bat` 即可。启动时会自动把 ComfyUI 作为子进程拉起来，**不需要手动开 ComfyUI、不需要手动点加载模型**。

---

## 说明几件事

**关于模型许可。** 出图模型基于 Qwen Research License，我用的社区转换版本许可状态更模糊，**商用请自行确认条款**。另外这个版本移除了内置安全检查器，输出完全取决于提示词——**使用者需要对生成内容负全部责任**，本地生成也不等于可以自由传播，对外发布前请自行做合规审查。项目保留了 `NSFW_FILTER` 开关和生成记录，建议按需开启。

**关于速度。** 6GB 显存是真能跑，但也是真的慢：单格约 18 分钟，一页四格就是一小时出头。如果你有 12GB 以上显存，把 `COMFYUI_EXTRA_ARGS` 里的 `--lowvram` 删掉、下载 Q5_K_M 或 Q6_K 档，体验会好很多。

**关于"完全离线"。** 只有安装和下载模型需要联网，运行时所有请求都指向 `127.0.0.1`。生成的图、剧本、数据库、导出的 PDF 全在本机，不留任何外发通道。

---

## 最后

这个项目最让我满意的不是"能出图"——而是从一段文字到一本装订好的 PDF，中间**没有一步需要我手工补**。角色长相、场景氛围、对白气泡、页码排版，全都是流水线自己走完的。

代码已经开源在 GitHub：**https://github.com/wyfirstname/qwenimage-comic**

如果你也在折腾本地 AI 绘画、或者只是想要一个不联网的漫画工具，欢迎拿去看看。项目还有一些粗糙的地方，尤其是低显存下的速度，Issue 和 PR 都很欢迎。

如果这个项目帮到了你，**给个 Star ⭐ 就是最大的鼓励**。

---

<div align="center">

### 支持作者 ☕

如果这个项目对你有帮助，欢迎请我喝杯咖啡

<table>
  <tr>
    <td align="center">
      <img src="https://cdn.jsdelivr.net/gh/wyfirstname/qwenimage-comic@main/assets/juanzeng.png" alt="微信赞赏码" width="220"><br>
      <sub><b>微信赞赏</b> · 金额随意 · 感谢支持</sub>
    </td>
    <td align="center">
      <img src="https://cdn.jsdelivr.net/gh/wyfirstname/qwenimage-comic@main/assets/gongzhonghao.jpg" alt="公众号二维码" width="190"><br>
      <sub><b>公众号</b> · 项目更新与教程</sub>
    </td>
  </tr>
</table>

</div>

---

*本项目采用 MIT License 开源；模型权重遵循其原始许可协议。*
