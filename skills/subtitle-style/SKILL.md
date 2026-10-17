---
name: subtitle-style
description: Apply the project subtitle gold template to ASS subtitles or Jianying drafts. Use when a video rough-cut workflow needs consistent Jinling font, white centered text, resolution-aware position, and black shadow parameters. Do not use for video trimming, transcript generation, or general caption copy editing.
metadata:
  short-description: Apply the gold subtitle style
---

# Subtitle Style Skill

本 Skill 只负责字幕样式，不负责视频裁剪、语音转录、字幕文本生成或剪辑节奏编排。由视频粗剪 Skill 在获得字幕时间轴后调用。

## 输入

调用方应提供：

- 视频宽度和高度；
- 已完成时间对齐的字幕源，例如 SRT、ASS 或字幕事件 JSON；
- 金样配置文件，默认使用 assets/subtitle_gold_template.json；
- 输出模式：ass 或 jianying。

## 固定规则

- 字体：金陵体；
- 字体颜色：白色；
- 水平和垂直对齐：居中；
- 缩放：100% / 100%；
- 阴影颜色：黑色；
- 阴影不透明度：70%；
- 阴影角度：-45°；
- ASS 的阴影参数是近似映射，不能声称与剪映渲染完全一致；
- 模板中的“距离 5°”按“距离 5 px、角度 -45°”处理；如果调用方提供明确单位，应优先使用调用方说明。

## 工作流程

1. 读取并校验金样 JSON；
2. 根据视频宽高选择已配置分辨率，或按 1080×1920 的比例缩放；
3. 生成 ASS 样式参数和事件标签；
4. ass 模式返回 style_line、位置和阴影标签，交给 FFmpeg/字幕烧录工具；
5. jianying 模式返回剪映样式参数，由上层视频粗剪 Skill 使用剪映预设或 style_reference 继承真实字幕样式；
6. 输出结构化校验结果。

## 调用接口


auto 调用：

```powershell
python scripts/apply_subtitle_style.py --width 1080 --height 1920 --mode ass --out .subtitle_style.json
```

返回结果至少包含 resolution、scale、jianying、ass 和 checks。

上层视频粗剪 Skill 不应自行重写字体、颜色、阴影和坐标参数。需要改变样式时，应修改金样 JSON，并重新生成样式结果。

## 输出边界

本 Skill 输出样式配置，不直接创建剪映草稿，也不直接调用 FFmpeg。视频粗剪 Skill 负责把结果接入字幕烧录、剪映草稿生成和最终验证。

剪映可编辑草稿模式应优先使用剪映预设中的明文样式参考，通过 style_reference 克隆样式；ASS 参数只用于预览、烧录或兼容工程输出。
