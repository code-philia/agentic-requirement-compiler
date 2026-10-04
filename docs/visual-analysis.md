# Visual Reference Analysis

本文档记录当前编译器对 UI 视觉参考图进行分析时使用的提示词、输入格式、输出格式和处理规则。

实现位置：`src/core/visual_analysis.py`。节点上下文在 `src/agents/context/pipeline.py` 中读取结构化结果。

## 1. System Prompt

当前使用的完整提示词如下：

```text
You are a senior visual-design analyst.
Analyze one UI reference image and return only directly observable evidence that
can guide a production frontend implementation. Capture the whole visual system, not only controls and text.

- regions: ordered page regions and their content purpose from top to bottom.
- visible_controls: control type, visible label, placement, and important visual state.
- layout_cues: composition, container proportions, grid/columns, alignment, grouping, whitespace rhythm, density,
  hierarchy, and relationships between regions. Use relative measurements such as narrow/wide or compact/generous.
- style_cues: concrete reusable observations. Prefix each cue with the most fitting category among Color,
  Typography, Spacing, Surface, Border, Shape, Elevation, Iconography, or Imagery. Include approximate visible color
  values when reliable, font character/weight/scale relationships, corner treatment, border weight, and shadows.
- text_cues: meaningful visible copy in reading order, preserving valid Unicode only when confidently legible.

Describe what should be referenced, not everything that happens to appear in the image. The generated product must
retain its own requirement data and behavior, so do not infer hidden behavior or copy unrelated names, records, or
decorative content. Do not emit corrupted OCR text; omit uncertain text instead. Do not generate JSX, DOM, CSS,
Tailwind classes, source code, routes, API contracts, or component names. Copy reference_id exactly from the supplied
metadata. Use concise, implementation-useful strings and [] when a category has no reliable observation. Return only
the structured JSON object required by the supplied schema.
```

## Output Schema

模型使用的 `VISUAL_ANALYSIS_SCHEMA` 是严格 JSON Object，禁止额外字段：

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "reference_id",
    "regions",
    "visible_controls",
    "layout_cues",
    "style_cues",
    "text_cues"
  ],
  "properties": {
    "reference_id": {
      "type": "string",
      "pattern": "^VISUAL\\.[0-9a-f]{16}$"
    },
    "regions": {
      "type": "array",
      "items": {"type": "string", "minLength": 1},
      "maxItems": 64
    },
    "visible_controls": {
      "type": "array",
      "items": {"type": "string", "minLength": 1},
      "maxItems": 64
    },
    "layout_cues": {
      "type": "array",
      "items": {"type": "string", "minLength": 1},
      "maxItems": 64
    },
    "style_cues": {
      "type": "array",
      "items": {"type": "string", "minLength": 1},
      "maxItems": 64
    },
    "text_cues": {
      "type": "array",
      "items": {"type": "string", "minLength": 1},
      "maxItems": 64
    }
  }
}
```

其中每个观察字段都是字符串数组：

| 字段 | 含义 |
| --- | --- |
| `reference_id` | 当前图片的稳定 ID，必须与输入完全一致 |
| `regions` | 从上到下排列的页面区域及其内容用途 |
| `visible_controls` | 可见控件的类型、标签、位置和重要视觉状态 |
| `layout_cues` | 构图、容器比例、列网格、对齐、分组、留白、密度和层级 |
| `style_cues` | 可复用的颜色、字体、间距、表面、边框、形状、阴影、图标和图片观察 |
| `text_cues` | 按阅读顺序记录且能够可靠辨认的可见文字 |

所有数组都允许为空；每个数组最多 64 项。

## 输入、存储与读取

每次调用只分析一张图片。系统读取图片字节，以 data URL 发送图片，并同时提供 `reference_id`、图片名及上述 schema。`reference_id` 由图片内容 SHA-256 的前 16 位生成，格式为 `VISUAL.<16位小写十六进制>`。

返回值经过 JSON schema、ID 一致性及空白/损坏 Unicode 检查；无效结果最多纠正两次。API 使用严格 `json_schema` 返回格式，服务需支持该格式。

需求的 `visual_reference` 条目保存 `image_path`、`resolved_image_path`、`reference_id` 和对象类型的 `analysis`。缓存仍保存在 `.arc/visual_analysis_cache.json`，使用新的提示词版本隔离旧 Markdown 缓存；旧格式或图片内容变化后的分析会重新请求。下游直接读取 JSON 对象，不转换成 Python 字符串或解析 Markdown。分析失败时只保留图片路径，上下文标记为 `unavailable`，不会继续使用旧格式分析。

这些观察仅用于前端视觉实现。需求自己的业务数据和行为始终优先，`text_cues` 不作为数据库预置数据。
