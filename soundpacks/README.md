# 在线音效包维护

客户端从本仓库 `main` 分支的 `soundpacks/catalog.json` 获取列表，ZIP 文件独立存放在 `soundpacks-v1` Release。资源 Release 不作为主程序的最新版本。

## 添加或更新音效包

1. 将音频放入 `music/语音包名/` 并加入 Git 跟踪。构建脚本只收集已跟踪的音频，不会上传本地配置或未跟踪文件。
2. 编辑 `soundpacks/packs.json`：每个包有稳定的 `id`、本地文件夹 `name`、显示名 `title`、`description` 和独立 `version`。
3. 音频发生变化时必须提高该包的 `version`，然后运行 `python tools/build_soundpacks.py`。
4. 将 `dist/soundpacks/` 中新增的 ZIP 上传到 `soundpacks-v1` Release。已发布的同版本 ZIP 应保持不变。
5. ZIP 上传完成后，提交并推送音频、包定义和生成的 `soundpacks/catalog.json`。用户点击“刷新列表”即可获得新包，不需要更新主程序。

ZIP 内直接放音频文件，可包含子文件夹；不放最外层语音包文件夹。清单包含 ZIP 大小、SHA-256 和音频数量，客户端安装前会校验。

## 本地文件和更新

- 初次同步已有语音包时，相同内容直接复用；同名不同内容会另存为 `名称 (在线).扩展名`，保留原文件。
- 更新只替换内容仍与上次安装一致的在线文件。用户修改过的文件及自行添加的音频会保留。
- 当前更新采用合并方式，线上移除的旧音频会继续保留在本地。
- `.soundboard-pack.json` 记录安装版本和文件归属，不包含账号或凭据。
- 下载缓存和清单缓存保存在 `%APPDATA%/GameSoundboard/`。关闭窗口或主程序会等待取消操作完成，避免中途终止安装。
