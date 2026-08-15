import asyncio
import base64
import json
import os
import random
import re
import time
import traceback
import uuid
from urllib.parse import quote

import aiohttp

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Record, Reply, Video
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_BASE = os.path.join(get_astrbot_plugin_data_path(), "media_luna")
CHANNELS_FILE = os.path.join(DATA_BASE, "channels.json")
PRESETS_FILE = os.path.join(DATA_BASE, "presets.json")
TASKS_FILE = os.path.join(DATA_BASE, "tasks.json")
STATS_FILE = os.path.join(DATA_BASE, "stats.json")
MEDIA_DIR = os.path.join(DATA_BASE, "media")
MAX_TASKS = 500


def migrate_legacy_data(legacy: str | None = None, new_base: str | None = None) -> None:
    """将旧版本存放在插件目录内的运行时数据迁移到 data/plugin_data/media_luna。

    插件更新会重建插件目录，只有把数据放在 data/plugin_data 下才能保留。
    """
    legacy = legacy or PLUGIN_DIR
    new_base = new_base or DATA_BASE
    if os.path.abspath(legacy) == os.path.abspath(new_base):
        return
    os.makedirs(new_base, exist_ok=True)
    for name in ("channels.json", "presets.json", "tasks.json", "stats.json"):
        old = os.path.join(legacy, name)
        new = os.path.join(new_base, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                os.replace(old, new)
                logger.info(f"media-luna 数据已迁移: {old} -> {new}")
            except Exception as e:
                logger.warning(f"media-luna 数据迁移失败 {old}: {e}")
    old_media = os.path.join(legacy, "media")
    new_media = os.path.join(new_base, "media")
    if os.path.isdir(old_media) and not os.path.isdir(new_media):
        try:
            os.replace(old_media, new_media)
            logger.info("media-luna media 目录已迁移")
        except Exception as e:
            logger.warning(f"media-luna media 目录迁移失败: {e}")


def _field(
    key,
    label,
    type="text",
    default="",
    required=False,
    placeholder="",
    description="",
    options=None,
):
    return {
        "key": key,
        "label": label,
        "type": type,
        "default": default,
        "required": required,
        "placeholder": placeholder,
        "description": description,
        "options": options or [],
    }


# ==================== 连接器定义（与 Media Luna 字段对齐） ====================
CONNECTORS = {
    "dalle": {
        "name": "DALL-E / OpenAI",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://api.openai.com/v1/images/generations"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="nano-banana-2"),
            _field("size", "图片尺寸", required=True, default="1024x1024", placeholder="1024x1024 / 1792x1024 / 1024x1792"),
            _field("quality", "质量", default="standard", placeholder="standard / hd"),
            _field("style", "风格", default="vivid", placeholder="vivid / natural"),
            _field("n", "生成数量", "number", 1),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "chat-api": {
        "name": "Chat API（OpenAI 兼容）",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://api.openai.com/v1/images/generations"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="gpt-image-1"),
            _field("size", "图片尺寸", required=True, default="1024x1024"),
            _field("n", "生成数量", "number", 1),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "gemini": {
        "name": "Google Gemini",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://generativelanguage.googleapis.com"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="gemini-2.5-flash-image"),
            _field("numberOfImages", "生成数量", "number"),
            _field("aspectRatio", "宽高比", placeholder="1:1 / 3:4 / 4:3 / 9:16 / 16:9"),
            _field("imageSize", "图片尺寸", placeholder="0.5K / 1K / 2K / 4K"),
            _field("outputMimeType", "输出格式", "select", "", options=[
                {"label": "不设置（默认 JPEG）", "value": ""},
                {"label": "JPEG", "value": "image/jpeg"},
                {"label": "PNG", "value": "image/png"},
            ]),
            _field("forceImageOutput", "强制图片输出", "boolean", False),
            _field("thinkingLevel", "思考程度", "select", "", options=[
                {"label": "不设置", "value": ""},
                {"label": "高 (high)", "value": "high"},
                {"label": "中 (medium)", "value": "medium"},
                {"label": "低 (low)", "value": "low"},
            ]),
            _field("safetyLevel", "安全过滤级别", "select", "", options=[
                {"label": "不设置", "value": ""},
                {"label": "关闭 (OFF)", "value": "OFF"},
                {"label": "仅高风险", "value": "BLOCK_ONLY_HIGH"},
                {"label": "中等及以上", "value": "BLOCK_MEDIUM_AND_ABOVE"},
            ]),
            _field("timeout", "超时时间（秒）", "number", 600),
        ],
    },
    "sd-webui": {
        "name": "Stable Diffusion WebUI",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="http://127.0.0.1:7860"),
            _field("model", "模型（留空用当前加载）"),
            _field("sampler", "采样器", default="Euler a"),
            _field("steps", "步数", "number", 20),
            _field("cfgScale", "CFG Scale", "number", 7),
            _field("width", "宽度", "number", 512),
            _field("height", "高度", "number", 512),
            _field("negativePrompt", "负面提示词", "textarea", "lowres, bad anatomy, bad hands, text, error, missing fingers"),
            _field("batchSize", "批量大小", "number", 1),
            _field("seed", "种子", "number", -1, placeholder="-1 为随机"),
            _field("denoisingStrength", "去噪强度 (img2img)", "number", 0.75),
            _field("timeout", "超时时间（秒）", "number", 120),
        ],
    },
    "comfyui": {
        "name": "ComfyUI",
        "supportedTypes": ["image", "video"],
        "defaultTags": ["text2img", "img2img", "text2video", "img2video"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="http://127.0.0.1:8188"),
            _field("isSecureConnection", "使用安全连接", "boolean", False),
            _field("workflow", "默认工作流 (JSON)", "textarea", "", description="从 ComfyUI 导出的 API 格式，用 {{prompt}} 作为提示词占位符"),
            _field("promptNodeId", "Prompt 节点 ID（可选）", placeholder="如不使用 {{prompt}}，指定 CLIPTextEncode 节点 ID"),
            _field("imageCount", "接受图片数量", "number", 1),
            _field("imageNodeId1", "图片1 输入节点 ID（可选）"),
            _field("imageNodeId2", "图片2 输入节点 ID（可选）"),
            _field("imageNodeId3", "图片3 输入节点 ID（可选）"),
            _field("avoidCache", "避免缓存（随机化 seed）", "boolean", True),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "flux": {
        "name": "Flux (Replicate)",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("apiKey", "Replicate API Token", "password", required=True),
            _field("model", "模型", required=True, default="black-forest-labs/flux-1.1-pro"),
            _field("aspectRatio", "宽高比", default="1:1"),
            _field("outputFormat", "输出格式", default="png"),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "midjourney": {
        "name": "Midjourney (Proxy)",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://api.midjourneyapi.xyz/mj/v2"),
            _field("apiKey", "API Key", "password", required=True),
            _field("webhookUrl", "Webhook 回调地址"),
            _field("aspectRatio", "默认宽高比 (--ar)", placeholder="16:9 / 9:16 / 2:3"),
            _field("mode", "模式", "select", "", options=[
                {"label": "不设置", "value": ""},
                {"label": "Fast", "value": "fast"},
                {"label": "Relax", "value": "relax"},
                {"label": "Turbo", "value": "turbo"},
            ]),
            _field("timeout", "超时时间（秒）", "number", 600),
        ],
    },
    "stability": {
        "name": "Stability AI",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, placeholder="sd3-large / core"),
            _field("aspectRatio", "宽高比", "select", "1:1", options=[
                {"label": "1:1", "value": "1:1"}, {"label": "16:9", "value": "16:9"},
                {"label": "21:9", "value": "21:9"}, {"label": "2:3", "value": "2:3"},
                {"label": "3:2", "value": "3:2"}, {"label": "4:5", "value": "4:5"},
                {"label": "5:4", "value": "5:4"}, {"label": "9:16", "value": "9:16"},
                {"label": "9:21", "value": "9:21"},
            ]),
            _field("negativePrompt", "负面提示词", "textarea"),
            _field("seed", "种子", "number"),
            _field("outputFormat", "输出格式", "select", "png", options=[
                {"label": "PNG", "value": "png"}, {"label": "JPEG", "value": "jpeg"},
            ]),
            _field("timeout", "超时时间（秒）", "number", 60),
        ],
    },
    "doubao": {
        "name": "豆包 Seedream",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://api.volcengine.com/v1/images/generations"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="Doubao-Seedream-4.5"),
            _field("size", "图片尺寸", placeholder="2K / 4K / 2048x2048"),
            _field("scale", "文本影响程度", "number", placeholder="0-1"),
            _field("forceSingle", "强制单图", "boolean", False),
            _field("enableImageBase64", "返回 Base64", "boolean", False),
            _field("enableImageInput", "允许图片输入", "boolean", True),
            _field("timeout", "超时时间（秒）", "number", 600),
        ],
    },
    "novelai": {
        "name": "NovelAI",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("apiKey", "Token", "password", required=True),
            _field("model", "模型", required=True, default="nai-diffusion-3"),
            _field("width", "宽度", "number", 832),
            _field("height", "高度", "number", 1216),
            _field("scale", "Scale", "number", 11),
            _field("sampler", "采样器", default="k_euler_ancestral"),
            _field("steps", "步数", "number", 28),
            _field("negativePrompt", "负面提示词", "textarea"),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "pollinations": {
        "name": "Pollinations（免费）",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("width", "宽度", "number", 1024),
            _field("height", "高度", "number", 1024),
            _field("timeout", "超时时间（秒）", "number", 180),
        ],
    },
    "openai-video": {
        "name": "OpenAI Video (Sora)",
        "supportedTypes": ["video"],
        "defaultTags": ["text2video", "img2video"],
        "fields": [
            _field("apiUrl", "API Base URL", required=True, default="https://api.openai.com/v1"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="sora-2"),
            _field("size", "尺寸", placeholder="1280x720"),
            _field("seconds", "时长（秒）", "number"),
            _field("fps", "帧率", "number"),
            _field("seed", "种子", "number"),
            _field("enableImageInput", "允许图片输入", "boolean", True),
            _field("pollInterval", "轮询间隔（毫秒）", "number", 5000),
            _field("timeout", "超时时间（秒）", "number", 900),
        ],
    },
    "zhipu": {
        "name": "智谱 CogVideoX",
        "supportedTypes": ["video"],
        "defaultTags": ["text2video"],
        "fields": [
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="cogvideox-flash"),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "test": {
        "name": "测试（本地生成）",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("delay", "模拟耗时（秒）", "number", 1),
        ],
    },
    "agnes-image": {
        "name": "Agnes Image",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://apihub.agnes-ai.com/v1/images/generations"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="agnes-image-2.1-flash"),
            _field("size", "图片尺寸", placeholder="1024x768"),
            _field("responseFormat", "响应格式", "select", "url", options=[
                {"label": "Base64", "value": "b64_json"},
                {"label": "URL", "value": "url"},
            ]),
            _field("enableImageInput", "允许图片输入", "boolean", True),
            _field("timeout", "超时时间（秒）", "number", 600),
        ],
    },
    "agnes-video": {
        "name": "Agnes Video",
        "supportedTypes": ["video"],
        "defaultTags": ["text2video", "img2video"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://apihub.agnes-ai.com/v1/videos"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="agnes-video-v2.0"),
            _field("mode", "生成模式", "select", "", options=[
                {"label": "自动", "value": ""},
                {"label": "图生视频 (ti2vid)", "value": "ti2vid"},
                {"label": "关键帧 (keyframes)", "value": "keyframes"},
            ]),
            _field("width", "宽度", "number", 1152),
            _field("height", "高度", "number", 768),
            _field("numFrames", "帧数", "number", 121, description="必须 ≤441 且满足 8n+1"),
            _field("frameRate", "帧率", "number", 24),
            _field("numInferenceSteps", "推理步数", "number"),
            _field("seed", "种子", "number"),
            _field("negativePrompt", "负面提示词", "textarea"),
            _field("enableImageInput", "允许图片输入", "boolean", True),
            _field("pollInterval", "轮询间隔（毫秒）", "number", 5000),
            _field("timeout", "超时时间（秒）", "number", 900),
        ],
    },
    "byteplus-image": {
        "name": "BytePlus Seedream",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://ark.ap-southeast.bytepluses.com/api/v3/images/generations"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="seedream-4-0"),
            _field("size", "图片尺寸", placeholder="2048x2048 / 2K / 4K"),
            _field("seed", "种子", "number"),
            _field("sequentialImageGeneration", "批量关联出图", "select", "disabled", options=[
                {"label": "关闭", "value": "disabled"}, {"label": "自动", "value": "auto"},
            ]),
            _field("maxImages", "最大出图数", "number"),
            _field("guidanceScale", "提示词引导", "number"),
            _field("outputFormat", "输出格式", "select", "jpeg", options=[
                {"label": "JPEG", "value": "jpeg"}, {"label": "PNG", "value": "png"},
            ]),
            _field("responseFormat", "响应格式", "select", "url", options=[
                {"label": "URL", "value": "url"}, {"label": "Base64", "value": "b64_json"},
            ]),
            _field("watermark", "添加水印", "boolean", True),
            _field("optimizePromptMode", "提示词优化模式", "select", "", options=[
                {"label": "不设置", "value": ""}, {"label": "标准", "value": "standard"}, {"label": "快速", "value": "fast"},
            ]),
            _field("enableImageInput", "允许图片输入", "boolean", True),
            _field("timeout", "超时时间（秒）", "number", 600),
        ],
    },
    "custom-form-image": {
        "name": "Custom Form Image",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, placeholder="https://your-server.example.com/generate"),
            _field("userAgent", "User-Agent"),
            _field("acceptLanguage", "Accept-Language", default="zh-CN,zh;q=0.9,en-US;q=0.6,en;q=0.5"),
            _field("promptFieldName", "提示词字段名", default="prompt"),
            _field("negativePromptFieldName", "负面提示词字段名", default="negative_prompt"),
            _field("resolutionFieldName", "分辨率字段名", default="resolution"),
            _field("resolution", "分辨率", placeholder="832x1216"),
            _field("negativePrompt", "默认负面提示词", "textarea"),
            _field("extraFormFields", "额外表单字段(JSON)", "textarea", placeholder='{"foo":"bar"}'),
            _field("responseImageField", "返回图片字段", default="image"),
            _field("responseFilenameField", "返回文件名字段", default="filename"),
            _field("timeout", "超时时间（秒）", "number", 180),
            _field("extraHeaders", "额外请求头(JSON)", "textarea", placeholder='{"X-Foo":"bar"}'),
        ],
    },
    "modelscope": {
        "name": "ModelScope (魔搭)",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://api-inference.modelscope.cn"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="Tongyi-MAI/Z-Image-Turbo"),
            _field("loras", "LoRA 模型"),
            _field("negativePrompt", "负面提示词", "textarea"),
            _field("width", "宽度", "number", 1024),
            _field("height", "高度", "number", 1024),
            _field("numImages", "生成数量", "number", 1),
            _field("seed", "种子", "number"),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "newapi-video": {
        "name": "NewAPI Video",
        "supportedTypes": ["video"],
        "defaultTags": ["text2video", "img2video"],
        "fields": [
            _field("apiUrl", "API Base URL", required=True, default="https://api.example.com"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, placeholder="kling-v1 / jimeng-video"),
            _field("mode", "模式", placeholder="image2video / text2video"),
            _field("size", "尺寸", placeholder="1280x720"),
            _field("width", "宽度", "number"),
            _field("height", "高度", "number"),
            _field("duration", "时长（秒）", "number"),
            _field("fps", "帧率", "number"),
            _field("seed", "种子", "number"),
            _field("negativePrompt", "负面提示词", "textarea"),
            _field("enableImageInput", "允许图片输入", "boolean", True),
            _field("pollInterval", "轮询间隔（毫秒）", "number", 5000),
            _field("timeout", "超时时间（秒）", "number", 900),
        ],
    },
    "peinture": {
        "name": "Peinture 派奇智图",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://peinture.u14.app/api/v1/generate"),
            _field("apiKey", "API Key（可选）", "password"),
            _field("model", "模型", required=True, default="huggingface/flux-1-schnell", placeholder="huggingface/z-image-turbo 等"),
            _field("aspectRatio", "宽高比", placeholder="1:1 / 16:9 / 9:16"),
            _field("steps", "步数", "number"),
            _field("guidance", "引导强度", "number"),
            _field("timeout", "超时时间（秒）", "number", 120),
        ],
    },
    "runway": {
        "name": "Runway",
        "supportedTypes": ["video"],
        "defaultTags": ["text2video", "img2video"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://api.runwayml.com/v1"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, placeholder="gen-3-alpha / gen-2"),
            _field("duration", "时长 (秒)", "select", "5", options=[
                {"label": "5 秒", "value": "5"}, {"label": "10 秒", "value": "10"},
            ]),
            _field("aspectRatio", "宽高比", "select", "16:9", options=[
                {"label": "16:9", "value": "16:9"}, {"label": "9:16", "value": "9:16"},
            ]),
            _field("seed", "种子", "number"),
            _field("timeout", "超时时间（秒）", "number", 600),
        ],
    },
    "suno": {
        "name": "Suno AI 音乐",
        "supportedTypes": ["audio"],
        "defaultTags": ["text2audio"],
        "fields": [
            _field("apiUrl", "API URL", required=True, default="https://api.goapi.ai/suno/v1/music"),
            _field("apiKey", "API Key", "password", required=True),
            _field("instrumental", "纯音乐", "boolean", False),
            _field("tags", "音乐风格 (Tags)", placeholder="pop, upbeat, electronic"),
            _field("title", "标题"),
            _field("timeout", "超时时间（秒）", "number", 300),
        ],
    },
    "vertex-ai": {
        "name": "Google Vertex AI",
        "supportedTypes": ["image"],
        "defaultTags": ["text2img", "img2img"],
        "fields": [
            _field("apiEndpoint", "API Endpoint", required=True, default="aiplatform.googleapis.com"),
            _field("apiKey", "API Key", "password", required=True),
            _field("model", "模型", required=True, default="gemini-3-pro-image-preview"),
            _field("numberOfImages", "生成数量", "number"),
            _field("aspectRatio", "宽高比", placeholder="1:1 / 3:4 / 4:3 / 9:16 / 16:9"),
            _field("imageSize", "图片尺寸", "select", "", options=[
                {"label": "不设置", "value": ""}, {"label": "1024x1024 (1K)", "value": "1K"},
                {"label": "2048x2048 (2K)", "value": "2K"}, {"label": "4096x4096 (4K)", "value": "4K"},
            ]),
            _field("outputMimeType", "输出格式", "select", "", options=[
                {"label": "不设置", "value": ""}, {"label": "JPEG", "value": "image/jpeg"}, {"label": "PNG", "value": "image/png"},
            ]),
            _field("forceImageOutput", "强制图片输出", "boolean", True),
            _field("thinkingLevel", "思考程度", "select", "", options=[
                {"label": "不设置", "value": ""}, {"label": "高 (high)", "value": "high"},
                {"label": "中 (medium)", "value": "medium"}, {"label": "低 (low)", "value": "low"},
            ]),
            _field("filterThoughtImages", "过滤思考图片", "boolean", True),
            _field("textOnlyAsSuccess", "纯文字视为成功", "boolean", False),
            _field("safetyLevel", "安全过滤级别", "select", "", options=[
                {"label": "不设置", "value": ""}, {"label": "关闭 (OFF)", "value": "OFF"},
                {"label": "仅高风险", "value": "BLOCK_ONLY_HIGH"},
                {"label": "中等及以上", "value": "BLOCK_MEDIUM_AND_ABOVE"},
            ]),
            _field("timeout", "超时时间（秒）", "number", 600),
        ],
    },
}

IMAGE_CONNECTOR_IDS = [
    cid for cid, c in CONNECTORS.items() if "image" in c["supportedTypes"]
]
VIDEO_CONNECTOR_IDS = [
    cid for cid, c in CONNECTORS.items() if "video" in c["supportedTypes"]
]
AUDIO_CONNECTOR_IDS = [
    cid for cid, c in CONNECTORS.items() if "audio" in c["supportedTypes"]
]

# v1 渠道迁移映射：旧 type -> 新 connectorId
LEGACY_TYPE_MAP = {
    "openai": "dalle",
    "chat": "chat-api",
    "gemini": "gemini",
    "sd": "sd-webui",
    "sdwebui": "sd-webui",
    "comfy": "comfyui",
    "comfyui": "comfyui",
    "flux": "flux",
    "mj": "midjourney",
    "midjourney": "midjourney",
    "stability": "stability",
    "doubao": "doubao",
    "novelai": "novelai",
    "pollinations": "pollinations",
    "sora": "openai-video",
    "openai_video": "openai-video",
    "zhipu": "zhipu",
}


def default_channel_config(connector_id: str) -> dict:
    return {
        f["key"]: f.get("default")
        for f in CONNECTORS[connector_id]["fields"]
    }


# 默认渠道列表为空：需要哪个渠道就自己在 WebUI 添加
DEFAULT_CHANNELS = {}


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def guess_mime(url: str, fallback="image/png") -> str:
    ext = url.split(".")[-1].lower() if "." in url else ""
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "mp4": "video/mp4",
        "webm": "video/webm", "bmp": "image/bmp",
    }.get(ext, fallback)


class Main(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._io_lock = asyncio.Lock()
        self._web_runner = None
        self._web_site = None
        self._sync_task = None
        self._channel_cmd_handlers = []
        self._channel_regex_handler = None
        self._recent_generations: dict[str, float] = {}
        migrate_legacy_data()
        os.makedirs(MEDIA_DIR, exist_ok=True)
        self._migrate_channels()
        if not os.path.exists(CHANNELS_FILE):
            save_json(CHANNELS_FILE, DEFAULT_CHANNELS)
        if not os.path.exists(PRESETS_FILE):
            save_json(PRESETS_FILE, {})
        if not os.path.exists(TASKS_FILE):
            save_json(TASKS_FILE, [])
        if not os.path.exists(STATS_FILE):
            save_json(
                STATS_FILE,
                {"total": {"image": 0, "video": 0}, "services": {}, "users": {}},
            )

    def _migrate_channels(self) -> None:
        """将 v1 的 {type, model, size...} 渠道迁移为 v2 的 {connectorId, connectorConfig}"""
        channels = load_json(CHANNELS_FILE, None)
        if not isinstance(channels, dict):
            return
        changed = False
        for name, ch in channels.items():
            if isinstance(ch, dict) and "connectorId" not in ch and ch.get("type"):
                old_type = str(ch.get("type", "")).lower()
                cid = LEGACY_TYPE_MAP.get(old_type)
                if not cid:
                    continue
                cfg = default_channel_config(cid)
                for k in ("model", "size", "width", "height", "steps", "cfg", "sampler",
                          "negative", "api_key", "url", "base", "checkpoint",
                          "aspect_ratio", "output_format", "duration", "timeout"):
                    if k in ch:
                        cfg[k] = ch[k]
                ch["connectorId"] = cid
                ch["connectorConfig"] = cfg
                ch.setdefault("displayName", name)
                ch.setdefault("enabled", True)
                ch.setdefault("tags", list(CONNECTORS[cid].get("defaultTags", [])))
                for old_key in ("type", "model", "size", "width", "height", "steps", "cfg",
                                "sampler", "negative", "api_key", "url", "base", "checkpoint",
                                "aspect_ratio", "output_format", "duration", "timeout"):
                    ch.pop(old_key, None)
                changed = True
        if changed:
            save_json(CHANNELS_FILE, channels)

    def _cfg(self, key: str, default=""):
        try:
            return self.config.get(key, default)
        except AttributeError:
            return default

    def _save_plugin_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as e:
                logger.warning(f"media-luna 保存插件配置失败: {e}")

    def _save_dir(self) -> str:
        d = str(self._cfg("save_dir", "")).strip()
        if d:
            path = os.path.abspath(d)
            os.makedirs(path, exist_ok=True)
            return path
        os.makedirs(DATA_BASE, exist_ok=True)
        return DATA_BASE

    def channel_names(self) -> list[str]:
        return list(load_json(CHANNELS_FILE, {}).keys())

    def get_channel(self, name: str) -> dict | None:
        return load_json(CHANNELS_FILE, {}).get(name)

    def get_preset(self, name: str) -> dict | None:
        presets = load_json(PRESETS_FILE, {})
        p = presets.get(name)
        if p is None:
            for key, val in presets.items():
                if key.lower() == name.lower():
                    return val
            return None
        return p

    def _apply_preset(self, prompt: str) -> tuple[str, str | None, dict | None]:
        """识别「预设名 + 剩余文字」，返回 (最终提示词, 预设对象)"""
        presets = load_json(PRESETS_FILE, {})
        text = (prompt or "").strip()
        if not text:
            return text, None, None
        first = text.split(maxsplit=1)[0]
        if first in presets:
            preset = presets[first]
            if not preset.get("enabled", True):
                return text, None, None
            rest = text[len(first):].strip()
            template = preset.get("promptTemplate", "")
            if "{prompt}" in template:
                final = template.replace("{prompt}", rest)
            elif "{{userText}}" in template:
                final = template.replace("{{userText}}", rest)
            else:
                final = f"{template}\n\n{rest}" if rest else template
            return final.strip(), first, preset
        return text, None, None

    """==================== 文件与参考图 ===================="""

    async def _load_reference_file(self, ref: str) -> dict | None:
        """将参考图（URL / data URI / 本地路径 / media/xxx.png）转为 FileData"""
        try:
            ref = ref.strip()
            if not ref:
                return None
            if ref.startswith("http://") or ref.startswith("https://"):
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(ref, timeout=aiohttp.ClientTimeout(total=60)) as r:
                        if r.status != 200:
                            return None
                        raw = await r.read()
                return {"data": base64.b64encode(raw).decode(), "mime": guess_mime(ref)}
            if ref.startswith("data:"):
                m = re.match(r"data:([^;,]+)[^,]*,(.+)", ref, re.S)
                if m:
                    return {"data": m.group(2), "mime": m.group(1)}
                return None
            # 本地路径：相对路径相对于插件目录
            if not os.path.isabs(ref):
                path = os.path.join(PLUGIN_DIR, ref)
            else:
                path = ref
            if os.path.exists(path):
                with open(path, "rb") as f:
                    raw = f.read()
                return {"data": base64.b64encode(raw).decode(), "mime": guess_mime(path)}
        except Exception as e:
            logger.warning(f"media-luna 加载参考图失败 {ref}: {e}")
        return None

    async def _collect_reference_files(self, event, preset) -> list[dict]:
        """收集参考图：
        1. 预设自带的参考图
        2. 引用消息中被引用消息内的图片（只取图片，其余忽略）
        3. @ 用户 → 其头像
        4. 当前消息中附带的图片
        """
        files = []
        if preset:
            for ref in preset.get("referenceImages", []) or []:
                f = await self._load_reference_file(ref)
                if f:
                    files.append(f)

        messages = event.get_messages()

        # 引用消息：只取被引用消息内的图片
        for comp in messages:
            if isinstance(comp, Reply):
                for sub in comp.chain or []:
                    if isinstance(sub, Image):
                        try:
                            b64 = await sub.convert_to_base64()
                            files.append({"data": b64, "mime": "image/png"})
                        except Exception:
                            url = sub.url or (sub.file if str(sub.file or "").startswith("http") else "")
                            if url:
                                f = await self._load_reference_file(url)
                                if f:
                                    files.append(f)

        # @ 用户 → 获取其头像
        for comp in messages:
            if isinstance(comp, At):
                qq = str(comp.qq or "").strip()
                if qq and qq != "all":
                    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
                    f = await self._load_reference_file(avatar_url)
                    if f:
                        files.append(f)

        # 当前消息中附带的图片
        for comp in messages:
            if isinstance(comp, Image):
                try:
                    b64 = await comp.convert_to_base64()
                    files.append({"data": b64, "mime": "image/png"})
                except Exception as e:
                    logger.warning(f"media-luna 读取消息图片失败: {e}")
        return files

    """==================== 连接器生成 ===================="""

    async def _generate(self, chan: dict, prompt: str, files: list[dict], kind: str) -> list[dict]:
        cid = chan.get("connectorId", "")
        cfg = chan.get("connectorConfig", {}) or {}
        if kind == "image":
            if cid not in IMAGE_CONNECTOR_IDS:
                raise Exception(f"渠道 {chan.get('displayName', cid)} 不支持图片生成")
        elif kind == "video":
            if cid not in VIDEO_CONNECTOR_IDS:
                raise Exception(f"渠道 {chan.get('displayName', cid)} 不支持视频生成")
        elif kind == "audio":
            if cid not in AUDIO_CONNECTOR_IDS:
                raise Exception(f"渠道 {chan.get('displayName', cid)} 不支持音频生成")
        else:
            raise Exception(f"不支持的生成类型: {kind}")
        method = f"_gen_{cid.replace('-', '_')}"
        handler = getattr(self, method, None)
        if not handler:
            raise Exception(f"未实现的连接器: {cid}")
        return await handler(cfg, files, prompt)

    async def _post_json(self, url, json_body=None, headers=None, timeout=120):
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                url, json=json_body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    msg = text[:300]
                    try:
                        obj = json.loads(text)
                        if isinstance(obj, dict):
                            err = obj.get("error")
                            if isinstance(err, dict) and err.get("message"):
                                msg = err["message"]
                            elif isinstance(err, str) and err:
                                msg = err
                            elif obj.get("message"):
                                msg = obj["message"]
                    except Exception:
                        pass
                    raise Exception(f"HTTP {resp.status}: {msg}")
                return json.loads(text) if text else {}

    async def _poll(self, url, headers=None, status_keys=("status",), success=("success",),
                    fail_words=("fail", "error"), interval=3, timeout=300):
        deadline = time.time() + timeout
        async with aiohttp.ClientSession(trust_env=True) as session:
            while time.time() < deadline:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        raise Exception(f"轮询失败 HTTP {resp.status}")
                    data = await resp.json()
                status = ""
                for k in status_keys:
                    if isinstance(data, dict) and data.get(k) is not None:
                        status = str(data[k]).lower()
                        break
                if status in success:
                    return data
                if status and any(w in status for w in fail_words):
                    raise Exception(f"任务失败: {json.dumps(data, ensure_ascii=False)[:300]}")
                await asyncio.sleep(interval)
        raise Exception("任务超时")

    async def _gen_dalle(self, cfg, files, prompt):
        url = str(cfg.get("apiUrl") or "https://api.openai.com/v1/images/generations")
        model = cfg.get("model", "dall-e-3")
        body = {
            "model": model,
            "prompt": prompt,
            "size": cfg.get("size", "1024x1024"),
            "n": int(cfg.get("n", 1) or 1),
        }
        if model == "dall-e-3":
            body["quality"] = cfg.get("quality", "standard")
            body["style"] = cfg.get("style", "vivid")
        headers = {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"}
        data = await self._post_json(url, body, headers, int(cfg.get("timeout", 300) or 300))
        if not data.get("data") or not isinstance(data["data"], list):
            raise Exception(f"OpenAI API 返回格式异常: {json.dumps(data, ensure_ascii=False)[:300]}")
        assets = []
        for item in data["data"]:
            if item.get("url"):
                assets.append({"kind": "image", "url": item["url"], "mime": "image/png"})
            elif item.get("b64_json"):
                assets.append({"kind": "image", "url": f"data:image/png;base64,{item['b64_json']}", "mime": "image/png"})
        if not assets:
            raise Exception("OpenAI API 未返回图片")
        return assets

    async def _gen_chat_api(self, cfg, files, prompt):
        return await self._gen_dalle(cfg, files, prompt)

    async def _gen_gemini(self, cfg, files, prompt):
        api_key = cfg.get("apiKey", "")
        model = cfg.get("model", "gemini-2.5-flash-image")
        base = str(cfg.get("apiUrl") or "https://generativelanguage.googleapis.com").rstrip("/")
        if "/v1beta" not in base:
            base = f"{base}/v1beta"
        url = f"{base}/models/{model}:generateContent?key={api_key}"
        parts = []
        for f in files:
            if f.get("data"):
                parts.append({"inlineData": {"mimeType": f.get("mime", "image/png"), "data": f["data"]}})
        parts.append({"text": prompt})
        gen_cfg = {}
        if cfg.get("forceImageOutput"):
            gen_cfg["responseModalities"] = ["IMAGE"]
        image_config = {}
        if cfg.get("aspectRatio"):
            image_config["aspectRatio"] = cfg["aspectRatio"]
        if cfg.get("imageSize"):
            image_config["imageSize"] = str(cfg["imageSize"]).strip()
        if cfg.get("outputMimeType"):
            image_config["imageOutputOptions"] = {"mimeType": cfg["outputMimeType"]}
        if image_config:
            gen_cfg["imageConfig"] = image_config
        try:
            n = int(cfg.get("numberOfImages") or 1)
            if n > 1:
                gen_cfg["candidateCount"] = n
        except (TypeError, ValueError):
            pass
        if cfg.get("thinkingLevel"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": cfg["thinkingLevel"]}
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen_cfg}
        if cfg.get("safetyLevel"):
            body["safetySettings"] = [
                {"category": c, "threshold": cfg["safetyLevel"]}
                for c in ("HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT",
                          "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT")
            ]
        data = await self._post_json(
            url, body, {"Content-Type": "application/json"},
            int(cfg.get("timeout", 600) or 600),
        )
        assets = []
        for candidate in data.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                if part.get("thought"):
                    continue
                if part.get("inlineData"):
                    mime = part["inlineData"].get("mimeType", "image/png")
                    assets.append({"kind": "image", "url": f"data:{mime};base64,{part['inlineData']['data']}", "mime": mime})
                if part.get("text"):
                    assets.append({"kind": "text", "content": part["text"]})
        if not assets:
            raise Exception("Gemini 未返回内容")
        return assets

    async def _gen_sd_webui(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "http://127.0.0.1:7860").rstrip("/")
        image_files = [f for f in files if f.get("mime", "").startswith("image/")]
        body = {
            "prompt": prompt,
            "negative_prompt": cfg.get("negativePrompt", ""),
            "sampler_name": cfg.get("sampler", "Euler a"),
            "steps": int(cfg.get("steps", 20) or 20),
            "cfg_scale": float(cfg.get("cfgScale", 7) or 7),
            "width": int(cfg.get("width", 512) or 512),
            "height": int(cfg.get("height", 512) or 512),
            "batch_size": int(cfg.get("batchSize", 1) or 1),
            "seed": int(cfg.get("seed", -1) or -1),
        }
        if cfg.get("model"):
            body["override_settings"] = {"sd_model_checkpoint": cfg["model"]}
        if image_files:
            body["init_images"] = [f"data:{f['mime']};base64,{f['data']}" for f in image_files]
            body["denoising_strength"] = float(cfg.get("denoisingStrength", 0.75) or 0.75)
            url = f"{base}/sdapi/v1/img2img"
        else:
            url = f"{base}/sdapi/v1/txt2img"
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                url, json=body, timeout=aiohttp.ClientTimeout(total=int(cfg.get("timeout", 120) or 120))
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise Exception(f"SD WebUI 错误 {resp.status}: {text[:300]}")
                data = json.loads(text)
        images = data.get("images") or []
        if not images:
            raise Exception("SD WebUI 未返回图片")
        assets = []
        for img in images:
            b64 = img.split(",", 1)[1] if "," in img else img
            assets.append({"kind": "image", "url": f"data:image/png;base64,{b64}", "mime": "image/png"})
        return assets

    async def _gen_comfyui(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "http://127.0.0.1:8188").rstrip("/")
        scheme = "https" if cfg.get("isSecureConnection") else "http"
        base = base.replace("http://", f"{scheme}://").replace("https://", f"{scheme}://")
        workflow_str = cfg.get("workflow", "")
        if not workflow_str:
            raise Exception("未配置 ComfyUI 工作流")
        try:
            workflow = json.loads(workflow_str)
        except json.JSONDecodeError:
            raise Exception("工作流 JSON 格式无效")
        # 随机化 seed 避免缓存
        if cfg.get("avoidCache", True):
            seed = random.randint(0, 10**14)
            for node in workflow.values():
                if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                    if "seed" in node["inputs"]:
                        node["inputs"]["seed"] = seed
                    if "noise_seed" in node["inputs"]:
                        node["inputs"]["noise_seed"] = seed
        # 注入提示词
        workflow_str = json.dumps(workflow)
        if "{{prompt}}" in workflow_str:
            workflow = json.loads(workflow_str.replace("{{prompt}}", prompt.replace("\\", "\\\\").replace('"', '\\"')))
        else:
            node_id = cfg.get("promptNodeId")
            if not node_id:
                for k, node in workflow.items():
                    if node.get("class_type") == "CLIPTextEncode":
                        node_id = k
                        break
            if node_id and node_id in workflow:
                workflow[node_id]["inputs"]["text"] = prompt
        # 上传参考图并注入 LoadImage 节点
        image_files = [f for f in files if f.get("mime", "").startswith("image/")]
        if image_files:
            image_node_ids = [cfg.get("imageNodeId1"), cfg.get("imageNodeId2"), cfg.get("imageNodeId3")]
            load_nodes = [k for k, n in workflow.items() if n.get("class_type") == "LoadImage"]
            uploaded = []
            async with aiohttp.ClientSession(trust_env=True) as session:
                for i, img in enumerate(image_files):
                    form = aiohttp.FormData()
                    form.add_field("image", base64.b64decode(img["data"]), filename=f"input_{int(time.time())}_{i}.png",
                                   content_type=img.get("mime", "image/png"))
                    form.add_field("overwrite", "true")
                    form.add_field("type", "input")
                    async with session.post(f"{base}/upload/image", data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        if resp.status != 200:
                            raise Exception(f"ComfyUI 图片上传失败: {resp.status}")
                        rj = await resp.json()
                        uploaded.append(rj.get("name") or f"input_{int(time.time())}_{i}.png")
                for i, fname in enumerate(uploaded):
                    target = image_node_ids[i] if i < len(image_node_ids) and image_node_ids[i] else (
                        load_nodes[i] if i < len(load_nodes) else None
                    )
                    if target and target in workflow:
                        workflow[target]["inputs"]["image"] = fname
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                f"{base}/prompt", json={"prompt": workflow},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise Exception(f"ComfyUI 错误 {resp.status}: {text[:300]}")
                data = json.loads(text)
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise Exception("ComfyUI 未返回 prompt_id")
            deadline = time.time() + int(cfg.get("timeout", 300) or 300)
            history = None
            while time.time() < deadline:
                async with session.get(f"{base}/history/{prompt_id}") as resp:
                    hist = await resp.json()
                if prompt_id in hist:
                    entry = hist[prompt_id]
                    if entry.get("outputs"):
                        history = entry
                        break
                await asyncio.sleep(2)
            if not history:
                raise Exception("ComfyUI 执行超时")
            assets = []
            for node in history["outputs"].values():
                for key in ("images", "videos", "gifs"):
                    for file_info in node.get(key, []) or []:
                        ftype = file_info.get("type", "output")
                        if ftype and ftype != "output":
                            continue
                        view = (
                            f"{base}/view?filename={quote(file_info.get('filename', ''))}"
                            f"&subfolder={quote(file_info.get('subfolder', ''))}&type={ftype}"
                        )
                        async with session.get(view, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                            raw = await resp.read()
                        ext = file_info.get("filename", "").split(".")[-1].lower()
                        if key == "videos":
                            mime = guess_mime(file_info.get("filename", "a.mp4"), "video/mp4")
                            kind = "video"
                        else:
                            mime = guess_mime(file_info.get("filename", "a.png"), "image/png")
                            kind = "image"
                        assets.append({"kind": kind, "url": f"data:{mime};base64,{base64.b64encode(raw).decode()}", "mime": mime})
            if not assets:
                raise Exception("ComfyUI 执行完成但未返回结果")
            return assets

    async def _gen_flux(self, cfg, files, prompt):
        key = cfg.get("apiKey", "")
        model = cfg.get("model", "black-forest-labs/flux-1.1-pro")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"input": {
            "prompt": prompt,
            "aspect_ratio": cfg.get("aspectRatio", "1:1"),
            "output_format": cfg.get("outputFormat", "png"),
        }}
        data = await self._post_json(
            f"https://api.replicate.com/v1/models/{model}/predictions",
            body, headers, 60,
        )
        pred_id = data.get("id")
        if not pred_id:
            raise Exception("Replicate 未返回 id")
        result = await self._poll(
            f"https://api.replicate.com/v1/predictions/{pred_id}",
            headers, ("status",), ("succeeded",), ("failed", "canceled"),
            interval=3, timeout=int(cfg.get("timeout", 300) or 300),
        )
        output = result.get("output") or []
        if isinstance(output, str):
            output = [output]
        assets = [{"kind": "image", "url": u, "mime": "image/png"} for u in output if isinstance(u, str)]
        if not assets:
            raise Exception("Replicate 未返回图片")
        return assets

    async def _gen_midjourney(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "https://api.midjourneyapi.xyz/mj/v2").rstrip("/")
        key = cfg.get("apiKey", "")
        headers = {"X-API-KEY": key, "Content-Type": "application/json"}
        full_prompt = prompt
        if cfg.get("aspectRatio") and "--ar" not in full_prompt:
            full_prompt += f" --ar {cfg['aspectRatio']}"
        body = {"prompt": full_prompt}
        if cfg.get("mode"):
            body["process_mode"] = cfg["mode"]
        if cfg.get("webhookUrl"):
            body["webhook_url"] = cfg["webhookUrl"]
        res = await self._post_json(f"{base}/imagine", body, headers, 60)
        task_id = res.get("task_id") or res.get("taskId") or res.get("id")
        if not task_id:
            raise Exception(f"MJ 任务提交失败: {json.dumps(res, ensure_ascii=False)[:300]}")
        async with aiohttp.ClientSession(trust_env=True) as session:
            deadline = time.time() + int(cfg.get("timeout", 600) or 600)
            while time.time() < deadline:
                await asyncio.sleep(3)
                try:
                    async with session.post(f"{base}/result", json={}, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        data = await resp.json()
                    status = str(data.get("status", "")).lower()
                    if status in ("finished", "success", "completed"):
                        url = (data.get("task_result") or {}).get("image_url") or data.get("imageUrl") or data.get("url")
                        if url:
                            return [{"kind": "image", "url": url, "mime": "image/png"}]
                    if status in ("failed", "error"):
                        raise Exception(f"MJ 任务失败: {json.dumps(data, ensure_ascii=False)[:300]}")
                except Exception as e:
                    if isinstance(e, Exception) and str(e).startswith("MJ 任务失败"):
                        raise
                    continue
        raise Exception("Midjourney 任务超时")

    async def _gen_stability(self, cfg, files, prompt):
        key = cfg.get("apiKey", "")
        model = cfg.get("model", "core")
        if model == "core":
            url = "https://api.stability.ai/v2beta/stable-image/generate/core"
        elif "ultra" in model:
            url = "https://api.stability.ai/v2beta/stable-image/generate/ultra"
        else:
            url = "https://api.stability.ai/v2beta/stable-image/generate/sd3"
        form = aiohttp.FormData()
        form.add_field("prompt", prompt)
        if cfg.get("aspectRatio"):
            form.add_field("aspect_ratio", cfg["aspectRatio"])
        if cfg.get("outputFormat"):
            form.add_field("output_format", cfg["outputFormat"])
        if cfg.get("negativePrompt"):
            form.add_field("negative_prompt", cfg["negativePrompt"])
        seed = cfg.get("seed")
        if seed not in (None, "", 0):
            form.add_field("seed", str(seed))
        image_file = next((f for f in files if f.get("mime", "").startswith("image/")), None)
        if image_file:
            form.add_field("image", base64.b64decode(image_file["data"]), content_type=image_file["mime"], filename="input.png")
            form.add_field("mode", "image-to-image")
            form.add_field("strength", "0.7")
        else:
            form.add_field("mode", "text-to-image")
        if model != "core" and "ultra" not in model:
            form.add_field("model", model)
        headers = {"Authorization": f"Bearer {key}", "Accept": "image/*"}
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                url, data=form, headers=headers,
                timeout=aiohttp.ClientTimeout(total=int(cfg.get("timeout", 60) or 60)),
            ) as resp:
                content = await resp.read()
                if resp.status != 200:
                    raise Exception(f"Stability 错误 {resp.status}: {content.decode('utf-8', 'ignore')[:300]}")
        mime = "image/jpeg" if cfg.get("outputFormat") == "jpeg" else "image/png"
        return [{"kind": "image", "url": f"data:{mime};base64,{base64.b64encode(content).decode()}", "mime": mime}]

    async def _gen_doubao(self, cfg, files, prompt):
        url = str(cfg.get("apiUrl") or "https://api.volcengine.com/v1/images/generations")
        model = cfg.get("model", "")
        if not model:
            raise Exception("模型名称未配置")
        inp = {"prompt": prompt}
        image_files = [f for f in files if f.get("mime", "").startswith("image/")]
        if cfg.get("enableImageInput", True) and image_files:
            imgs = [f"data:{f['mime']};base64,{f['data']}" for f in image_files[:10]]
            inp["image"] = imgs[0] if len(imgs) == 1 else imgs
        extra = {}
        if cfg.get("size"):
            extra["size"] = cfg["size"]
        if cfg.get("scale") not in (None, ""):
            extra["scale"] = float(cfg["scale"])
        if cfg.get("forceSingle") is not None:
            extra["force_single"] = bool(cfg["forceSingle"])
        provider = {}
        if cfg.get("enableImageBase64") is not None:
            provider["enable_image_base64"] = bool(cfg["enableImageBase64"])
        if provider:
            extra["provider"] = provider
        body = {"model": model, "input": inp}
        if extra:
            body["extra_body"] = extra
        data = await self._post_json(
            url, body,
            {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"},
            int(cfg.get("timeout", 600) or 600),
        )
        items = data.get("data") if isinstance(data.get("data"), list) else (
            [data.get("data")] if data.get("data") else ([data] if data.get("url") or data.get("b64_json") else [])
        )
        assets = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if item.get("url"):
                assets.append({"kind": "image", "url": item["url"], "mime": "image/png"})
            elif item.get("b64_json"):
                assets.append({"kind": "image", "url": f"data:image/png;base64,{item['b64_json']}", "mime": "image/png"})
        if not assets:
            raise Exception("豆包 API 未返回图片")
        return assets

    async def _gen_novelai(self, cfg, files, prompt):
        body = {
            "input": prompt,
            "model": cfg.get("model", "nai-diffusion-3"),
            "parameters": {
                "width": int(cfg.get("width", 832) or 832),
                "height": int(cfg.get("height", 1216) or 1216),
                "scale": float(cfg.get("scale", 11) or 11),
                "sampler": cfg.get("sampler", "k_euler_ancestral"),
                "steps": int(cfg.get("steps", 28) or 28),
                "n_samples": 1,
                "negative_prompt": cfg.get("negativePrompt", ""),
                "seed": random.randint(0, 2**31 - 1),
            },
        }
        headers = {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                "https://image.novelai.net/ai/generate-image", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=int(cfg.get("timeout", 300) or 300)),
            ) as resp:
                content = await resp.read()
                if resp.status != 200:
                    raise Exception(f"NovelAI 错误 {resp.status}: {content.decode('utf-8', 'ignore')[:300]}")
        return [{"kind": "image", "url": f"data:image/png;base64,{base64.b64encode(content).decode()}", "mime": "image/png"}]

    async def _gen_pollinations(self, cfg, files, prompt):
        w = int(cfg.get("width", 1024) or 1024)
        h = int(cfg.get("height", 1024) or 1024)
        seed = random.randint(0, 2**31 - 1)
        url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width={w}&height={h}&seed={seed}&nologo=true"
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=int(cfg.get("timeout", 180) or 180))) as resp:
                content = await resp.read()
                if resp.status != 200:
                    raise Exception(f"Pollinations 错误 {resp.status}")
                mime = resp.content_type or "image/png"
        return [{"kind": "image", "url": f"data:{mime};base64,{base64.b64encode(content).decode()}", "mime": mime}]

    async def _gen_openai_video(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"}
        body = {"model": cfg.get("model", "sora-2"), "prompt": prompt}
        if cfg.get("size"):
            body["size"] = cfg["size"]
        for k in ("seconds", "fps", "seed"):
            v = cfg.get(k)
            if v not in (None, "", 0):
                body[k] = int(float(v))
        image_files = [f for f in files if f.get("mime", "").startswith("image/")]
        if cfg.get("enableImageInput", True) and image_files:
            body["image"] = f"data:{image_files[0]['mime']};base64,{image_files[0]['data']}"
        data = await self._post_json(f"{base}/videos", body, headers, 60)
        video_id = data.get("id") or data.get("task_id") or data.get("taskId")
        if not video_id:
            raise Exception("OpenAI Video 未返回任务 id")
        interval = max(1, int(cfg.get("pollInterval", 5000) or 5000) // 1000)
        result = await self._poll(
            f"{base}/videos/{video_id}", headers,
            ("status",), ("completed", "succeeded", "success"),
            interval=interval, timeout=int(cfg.get("timeout", 900) or 900),
        )
        video_url = (
            result.get("video_url") or result.get("url")
            or (result.get("output")[0] if isinstance(result.get("output"), list) and result.get("output") else None)
            or (result.get("data")[0].get("url") if isinstance(result.get("data"), list) and result.get("data") else None)
        )
        if not video_url:
            try:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(
                        f"{base}/videos/{video_id}/content", headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status == 200:
                            raw = await resp.read()
                            video_url = f"data:video/mp4;base64,{base64.b64encode(raw).decode()}"
            except Exception:
                pass
        if not video_url:
            raise Exception("OpenAI Video 未返回视频链接")
        return [{"kind": "video", "url": video_url, "mime": "video/mp4"}]

    async def _gen_zhipu(self, cfg, files, prompt):
        headers = {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"}
        body = {"model": cfg.get("model", "cogvideox-flash"), "prompt": prompt}
        data = await self._post_json(
            "https://open.bigmodel.cn/api/paas/v4/videos/generations", body, headers, 60
        )
        video_id = data.get("id")
        if not video_id:
            raise Exception("智谱 API 未返回 id")
        result = await self._poll(
            f"https://open.bigmodel.cn/api/paas/v4/videos/generations/{video_id}",
            headers, ("task_status",), ("success",), ("fail", "canceled"),
            interval=3, timeout=int(cfg.get("timeout", 300) or 300),
        )
        results = result.get("video_result") or []
        if not results:
            raise Exception("智谱未返回视频")
        return [{"kind": "video", "url": results[0].get("url", ""), "mime": "video/mp4"}]

    async def _gen_test(self, cfg, files, prompt):
        """本地测试连接器：不访问网络，返回一张 1x1 图片"""
        delay = float(cfg.get("delay", 1) or 1)
        await asyncio.sleep(delay)
        png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
               "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        return [{"kind": "image", "url": f"data:image/png;base64,{png}", "mime": "image/png"}]

    async def _gen_agnes_image(self, cfg, files, prompt):
        url = str(cfg.get("apiUrl") or "https://apihub.agnes-ai.com/v1/images/generations")
        model = cfg.get("model", "agnes-image-2.1-flash")
        if not model:
            raise Exception("模型名称未配置")
        body = {"model": model, "prompt": prompt}
        if cfg.get("size"):
            body["size"] = cfg["size"]
        if cfg.get("responseFormat") == "b64_json":
            body["return_base64"] = True
        extra = {}
        if cfg.get("enableImageInput", True):
            imgs = [f"data:{f['mime']};base64,{f['data']}" for f in files if f.get("mime", "").startswith("image/")]
            if imgs:
                extra["image"] = imgs
        if cfg.get("responseFormat"):
            extra["response_format"] = cfg["responseFormat"]
        if extra:
            body["extra_body"] = extra
        data = await self._post_json(
            url, body,
            {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"},
            int(cfg.get("timeout", 600) or 600),
        )
        items = (
            data.get("data") if isinstance(data.get("data"), list)
            else ([data.get("data")] if isinstance(data.get("data"), dict) else ([data] if data.get("url") or data.get("b64_json") else []))
        )
        assets = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            u = it.get("url") or it.get("image_url") or it.get("output_url") or (
                f"data:image/png;base64,{it['b64_json']}" if it.get("b64_json") else None
            )
            if u:
                assets.append({"kind": "image", "url": u, "mime": "image/png"})
        if not assets:
            raise Exception("Agnes Image 未返回图片")
        return assets

    async def _gen_agnes_video(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "https://apihub.agnes-ai.com/v1/videos").rstrip("/")
        body = {"model": cfg.get("model", "agnes-video-v2.0"), "prompt": prompt}
        for k, v in (("width", cfg.get("width")), ("height", cfg.get("height")),
                     ("num_frames", cfg.get("numFrames")), ("num_inference_steps", cfg.get("numInferenceSteps")),
                     ("seed", cfg.get("seed")), ("frame_rate", cfg.get("frameRate"))):
            if v not in (None, "", 0):
                body[k] = int(float(v))
        if cfg.get("negativePrompt"):
            body["negative_prompt"] = cfg["negativePrompt"]
        mode = cfg.get("mode") or ""
        imgs = [f"data:{f['mime']};base64,{f['data']}" for f in files if f.get("mime", "").startswith("image/")] if cfg.get("enableImageInput", True) else []
        if len(imgs) == 1 and mode != "keyframes":
            body["image"] = imgs[0]
            if mode:
                body["mode"] = mode
        elif len(imgs) > 1:
            body["extra_body"] = {"image": imgs}
            if mode:
                body["extra_body"]["mode"] = mode
        elif mode:
            body["mode"] = mode
        data = await self._post_json(
            base, body,
            {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"},
            int(cfg.get("timeout", 900) or 900),
        )
        tid = data.get("id") or data.get("task_id") or data.get("taskId")
        if not tid:
            raise Exception("Agnes Video 未返回任务 id")
        interval = max(1, int(cfg.get("pollInterval", 5000) or 5000) // 1000)
        result = await self._poll(
            f"{base}/{quote(str(tid))}", {"Authorization": f"Bearer {cfg.get('apiKey', '')}"},
            ("status",), ("completed", "succeeded", "success"),
            interval=interval, timeout=int(cfg.get("timeout", 900) or 900),
        )
        u = result.get("video_url") or result.get("url")
        if not u:
            raise Exception("Agnes Video 未返回视频链接")
        return [{"kind": "video", "url": u, "mime": "video/mp4"}]

    async def _gen_byteplus_image(self, cfg, files, prompt):
        url = str(cfg.get("apiUrl") or "https://ark.ap-southeast.bytepluses.com/api/v3/images/generations")
        model = cfg.get("model", "seedream-4-0")
        if not model:
            raise Exception("模型名称未配置")
        fmt = cfg.get("responseFormat") or "url"
        body = {"model": model, "prompt": prompt, "response_format": fmt}
        size = str(cfg.get("size") or "").strip().lower()
        if size:
            m = re.match(r"^([1-4])k$", size)
            body["size"] = f"{m.group(1)}K" if m else size.replace(" ", "")
        if cfg.get("seed") not in (None, ""):
            body["seed"] = int(cfg["seed"])
        if cfg.get("guidanceScale") not in (None, ""):
            body["guidance_scale"] = float(cfg["guidanceScale"])
        if cfg.get("watermark") is not None:
            body["watermark"] = bool(cfg["watermark"])
        if cfg.get("outputFormat"):
            body["output_format"] = cfg["outputFormat"]
        if cfg.get("optimizePromptMode"):
            body["optimize_prompt_options"] = {"mode": cfg["optimizePromptMode"]}
        seq = cfg.get("sequentialImageGeneration") or "disabled"
        if seq != "disabled":
            body["sequential_image_generation"] = seq
            if cfg.get("maxImages") not in (None, ""):
                body["sequential_image_generation_options"] = {"max_images": int(cfg["maxImages"])}
        if cfg.get("enableImageInput", True):
            imgs = [f"data:{f['mime']};base64,{f['data']}" for f in files if f.get("mime", "").startswith("image/")][:14]
            if len(imgs) == 1:
                body["image"] = imgs[0]
            elif len(imgs) > 1:
                body["image"] = imgs
        data = await self._post_json(
            url, body,
            {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"},
            int(cfg.get("timeout", 600) or 600),
        )
        if data.get("error"):
            raise Exception(str(data["error"]))
        items = data.get("data") if isinstance(data.get("data"), list) else []
        assets = []
        for it in items or []:
            if not isinstance(it, dict) or it.get("error"):
                continue
            u = it.get("url") or (
                f"data:image/{it.get('output_format') or cfg.get('outputFormat') or 'jpeg'};base64,{it['b64_json']}"
                if it.get("b64_json") else None
            )
            if u:
                mime = "image/png" if (it.get("output_format") == "png" or cfg.get("outputFormat") == "png") else "image/jpeg"
                assets.append({"kind": "image", "url": u, "mime": mime})
        if not assets:
            raise Exception("BytePlus 未返回图片")
        return assets

    async def _gen_custom_form_image(self, cfg, files, prompt):
        url = str(cfg.get("apiUrl") or "").rstrip("/")
        if not url:
            raise Exception("API URL 未配置")
        if files:
            raise Exception("该连接器暂不支持图片输入")
        form = {cfg.get("promptFieldName") or "prompt": prompt}
        if cfg.get("negativePrompt"):
            form[cfg.get("negativePromptFieldName") or "negative_prompt"] = cfg["negativePrompt"]
        if cfg.get("resolution"):
            form[cfg.get("resolutionFieldName") or "resolution"] = cfg["resolution"]
        try:
            extra = json.loads(cfg.get("extraFormFields") or "{}") if cfg.get("extraFormFields") else {}
            if not isinstance(extra, dict):
                extra = {}
            form.update({str(k): str(v) for k, v in extra.items() if v is not None})
        except Exception as e:
            raise Exception(f"extraFormFields 不是合法 JSON: {e}")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
            "Accept-Language": cfg.get("acceptLanguage") or "zh-CN,zh;q=0.9,en-US;q=0.6,en;q=0.5",
        }
        try:
            extra_headers = json.loads(cfg.get("extraHeaders") or "{}") if cfg.get("extraHeaders") else {}
            if isinstance(extra_headers, dict):
                headers.update({str(k): str(v) for k, v in extra_headers.items()})
        except Exception as e:
            raise Exception(f"extraHeaders 不是合法 JSON: {e}")
        headers["User-Agent"] = cfg.get("userAgent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0"
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                url, data=form, headers=headers,
                timeout=aiohttp.ClientTimeout(total=int(cfg.get("timeout", 180) or 180)),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise Exception(f"表单接口错误 {resp.status}: {text[:300]}")
                try:
                    data = json.loads(text)
                except Exception:
                    raise Exception(f"响应不是 JSON: {text[:200]}")
        field = cfg.get("responseImageField") or "image"
        image_value = data.get(field) if isinstance(data, dict) else None
        if not image_value or not isinstance(image_value, str):
            raise Exception(f"响应中未找到图片字段: {field}")
        if not (image_value.startswith("http") or image_value.startswith("data:")):
            if "://" in url:
                base_url = url[:url.index("/", url.index("://") + 2)]
            else:
                base_url = url
            image_value = base_url + ("" if image_value.startswith("/") else "/") + image_value
        return [{"kind": "image", "url": image_value, "mime": "image/png"}]

    async def _gen_modelscope(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "https://api-inference.modelscope.cn").rstrip("/")
        key = cfg.get("apiKey", "")
        model = cfg.get("model", "Tongyi-MAI/Z-Image-Turbo")
        body = {
            "model": model,
            "prompt": prompt,
            "n": int(cfg.get("numImages", 1) or 1),
            "size": f"{int(cfg.get('width', 1024) or 1024)}x{int(cfg.get('height', 1024) or 1024)}",
        }
        if cfg.get("negativePrompt"):
            body["negative_prompt"] = cfg["negativePrompt"]
        if cfg.get("seed") not in (None, ""):
            body["seed"] = int(cfg["seed"])
        if cfg.get("loras"):
            try:
                body["loras"] = json.loads(cfg["loras"])
            except Exception:
                body["loras"] = cfg["loras"]
        img = next((f for f in files if f.get("mime", "").startswith("image/")), None)
        if img:
            body["image"] = f"data:{img['mime']};base64,{img['data']}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-ModelScope-Async-Mode": "true"}
        data = await self._post_json(f"{base}/v1/images/generations", body, headers, 60)
        tid = data.get("task_id")
        if not tid:
            raise Exception(f"ModelScope 响应异常: {json.dumps(data, ensure_ascii=False)[:200]}")
        result = await self._poll(
            f"{base}/v1/tasks/{tid}",
            {"Authorization": f"Bearer {key}", "X-ModelScope-Task-Type": "image_generation"},
            ("task_status",), ("succeed",), interval=3, timeout=int(cfg.get("timeout", 300) or 300),
        )
        urls = result.get("output_images") or []
        if not urls:
            raise Exception("ModelScope 任务成功但未返回图片")
        return [{"kind": "image", "url": u, "mime": "image/jpeg"} for u in urls if isinstance(u, str)]

    async def _gen_newapi_video(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "").rstrip("/")
        model = cfg.get("model", "")
        if not model:
            raise Exception("模型名称未配置")
        if "/v1/video/generations" in base:
            base = base[:base.index("/v1/video/generations")]
        body = {"model": model, "prompt": prompt}
        for k, v in (("mode", cfg.get("mode")), ("size", cfg.get("size"))):
            if v:
                body[k] = v
        for k, ck in (("width", "width"), ("height", "height"), ("duration", "duration"), ("fps", "fps"), ("seed", "seed")):
            v = cfg.get(ck)
            if v not in (None, "", 0):
                body[k] = int(float(v))
        if cfg.get("negativePrompt"):
            body["negative_prompt"] = cfg["negativePrompt"]
        if cfg.get("enableImageInput", True):
            imgs = [f"data:{f['mime']};base64,{f['data']}" for f in files if f.get("mime", "").startswith("image/")]
            if len(imgs) == 1:
                body["image"] = imgs[0]
            elif len(imgs) > 1:
                body["image"] = imgs
        headers = {"Authorization": f"Bearer {cfg.get('apiKey', '')}", "Content-Type": "application/json"}
        data = await self._post_json(f"{base}/v1/video/generations", body, headers, 60)
        tid = data.get("id") or data.get("task_id") or data.get("taskId") or (data.get("data") or {}).get("id")
        if not tid:
            raise Exception("NewAPI Video 未返回任务 id")
        interval = max(1, int(cfg.get("pollInterval", 5000) or 5000) // 1000)
        result = await self._poll(
            f"{base}/v1/video/generations/{tid}", headers,
            ("status",), ("completed", "succeeded", "success"),
            interval=interval, timeout=int(cfg.get("timeout", 900) or 900),
        )
        u = (result.get("video_url") or result.get("url") or result.get("result_url")
             or (result.get("data") or {}).get("video_url") or (result.get("data") or {}).get("url"))
        if not u and isinstance(result.get("data"), list) and result["data"] and isinstance(result["data"][0], dict):
            u = result["data"][0].get("url")
        if not u and isinstance(result.get("output"), list) and result["output"]:
            u = result["output"][0]
        if not u:
            raise Exception("NewAPI Video 未返回视频链接")
        return [{"kind": "video", "url": u, "mime": "video/mp4"}]

    async def _gen_peinture(self, cfg, files, prompt):
        url = str(cfg.get("apiUrl") or "https://peinture.u14.app/api/v1/generate")
        model = cfg.get("model", "")
        if not model:
            raise Exception("模型未配置")
        body = {"model": model, "prompt": prompt}
        if cfg.get("aspectRatio"):
            body["ar"] = cfg["aspectRatio"]
        if cfg.get("steps") not in (None, ""):
            body["steps"] = int(cfg["steps"])
        if cfg.get("guidance") not in (None, ""):
            body["guidance"] = float(cfg["guidance"])
        headers = {"Content-Type": "application/json"}
        if cfg.get("apiKey"):
            headers["Authorization"] = f"Bearer {cfg['apiKey']}"
        data = await self._post_json(url, body, headers, int(cfg.get("timeout", 120) or 120))
        u = data.get("url") if isinstance(data, dict) else None
        if not u:
            raise Exception("Peinture 未返回图片 URL")
        return [{"kind": "image", "url": u, "mime": guess_mime(u)}]

    async def _gen_runway(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "https://api.runwayml.com/v1").rstrip("/")
        key = cfg.get("apiKey", "")
        model = cfg.get("model", "")
        if not model:
            raise Exception("模型未配置")
        params = {}
        if cfg.get("duration") not in (None, ""):
            params["durationSeconds"] = int(float(cfg["duration"]))
        if cfg.get("aspectRatio"):
            params["aspectRatio"] = cfg["aspectRatio"]
        if cfg.get("seed") not in (None, "", 0):
            params["seed"] = int(cfg["seed"])
        body = {"promptText": prompt, "model": model, "parameters": params}
        img = next((f for f in files if f.get("mime", "").startswith("image/")), None)
        if img:
            body["promptImage"] = f"data:{img['mime']};base64,{img['data']}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        data = await self._post_json(f"{base}/tasks", body, headers, 60)
        tid = data.get("id") or data.get("taskId")
        if not tid:
            raise Exception("Runway 未返回任务 id")
        result = await self._poll(
            f"{base}/tasks/{tid}", {"Authorization": f"Bearer {key}"},
            ("status",), ("succeeded",), interval=5, timeout=int(cfg.get("timeout", 600) or 600),
        )
        u = result.get("url") or (result.get("output") or [None])[0]
        if not u:
            raise Exception("Runway 任务成功但未返回视频")
        return [{"kind": "video", "url": u, "mime": "video/mp4"}]

    async def _gen_suno(self, cfg, files, prompt):
        base = str(cfg.get("apiUrl") or "https://api.goapi.ai/suno/v1/music").rstrip("/")
        key = cfg.get("apiKey", "")
        body = {"make_instrumental": bool(cfg.get("instrumental", False)), "wait_audio": False}
        if cfg.get("tags"):
            body["prompt"] = prompt
            body["tags"] = cfg["tags"]
            body["mv"] = "chirp-v3-0"
            if cfg.get("title"):
                body["title"] = cfg["title"]
        else:
            body["gpt_description_prompt"] = prompt
        headers = {"Content-Type": "application/json", "X-API-KEY": key, "Authorization": f"Bearer {key}"}
        data = await self._post_json(f"{base}/generate", body, headers, 60)
        ids = (
            data.get("ids") if isinstance(data.get("ids"), list)
            else ([data["task_id"]] if data.get("task_id") else [])
        )
        if not ids:
            raise Exception(f"Suno 响应异常: {json.dumps(data, ensure_ascii=False)[:200]}")
        deadline = time.time() + int(cfg.get("timeout", 300) or 300)
        assets = []
        async with aiohttp.ClientSession(trust_env=True) as session:
            while time.time() < deadline and len(assets) < len(ids):
                await asyncio.sleep(5)
                try:
                    async with session.get(
                        f"{base}/feed?ids={','.join(str(i) for i in ids)}", headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        rj = await resp.json()
                    clips = rj if isinstance(rj, list) else (rj.get("clips") or rj.get("data") or [])
                    for clip in clips or []:
                        if not isinstance(clip, dict):
                            continue
                        st = str(clip.get("status") or clip.get("state") or "").lower()
                        if st in ("complete", "streaming") and clip.get("audio_url"):
                            assets.append({"kind": "audio", "url": clip["audio_url"], "mime": "audio/mpeg"})
                except Exception:
                    continue
        if not assets:
            raise Exception("Suno 任务超时或失败")
        return assets

    async def _gen_vertex_ai(self, cfg, files, prompt):
        endpoint = str(cfg.get("apiEndpoint") or "aiplatform.googleapis.com").replace("https://", "").replace("http://", "").rstrip("/")
        key = cfg.get("apiKey", "")
        model = cfg.get("model", "")
        if not endpoint or not key or not model:
            raise Exception("Vertex AI 配置不完整")
        url = f"https://{endpoint}/v1/publishers/google/models/{model}:generateContent?key={key}"
        parts = [
            {"inlineData": {"mimeType": f.get("mime", "image/png"), "data": f["data"]}}
            for f in files if f.get("data")
        ] + [{"text": prompt}]
        gen = {}
        if cfg.get("forceImageOutput", True):
            gen["responseModalities"] = ["TEXT", "IMAGE"]
        ic = {}
        if cfg.get("aspectRatio"):
            ic["aspectRatio"] = cfg["aspectRatio"]
        if cfg.get("imageSize"):
            ic["imageSize"] = cfg["imageSize"]
        if cfg.get("outputMimeType"):
            ic["imageOutputOptions"] = {"mimeType": cfg["outputMimeType"]}
        if ic:
            gen["imageConfig"] = ic
        try:
            n = int(cfg.get("numberOfImages") or 1)
            if n > 1:
                gen["candidateCount"] = n
        except (TypeError, ValueError):
            pass
        if cfg.get("thinkingLevel"):
            gen["thinkingConfig"] = {"thinkingLevel": cfg["thinkingLevel"]}
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
        if cfg.get("safetyLevel"):
            body["safetySettings"] = [
                {"category": c, "threshold": cfg["safetyLevel"]}
                for c in ("HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT",
                          "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT")
            ]
        data = await self._post_json(url, body, {"Content-Type": "application/json"}, int(cfg.get("timeout", 600) or 600))
        assets = []
        for cand in data.get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                if part.get("thought") and cfg.get("filterThoughtImages", True):
                    continue
                if part.get("inlineData"):
                    mime = part["inlineData"].get("mimeType", "image/png")
                    assets.append({"kind": "image", "url": f"data:{mime};base64,{part['inlineData']['data']}", "mime": mime})
                if part.get("text"):
                    assets.append({"kind": "text", "content": part["text"]})
        if not assets:
            raise Exception("Vertex AI 未返回内容")
        has_media = any(a["kind"] != "text" for a in assets)
        if not has_media and not cfg.get("textOnlyAsSuccess", False):
            raise Exception("模型返回纯文字（无图片）")
        return assets

    """==================== 输出保存与记录 ===================="""

    async def _save_output(self, asset: dict, channel_name: str, idx: int) -> str | None:
        """将图片/视频保存到 save_dir（默认插件目录），返回本地路径"""
        url = asset.get("url", "")
        mime = asset.get("mime", "image/png")
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
               "image/webp": "webp", "image/bmp": "bmp", "video/mp4": "mp4",
               "video/webm": "webm", "audio/mpeg": "mp3", "audio/wav": "wav",
               "audio/flac": "flac", "audio/pcm": "pcm"}.get(mime, "bin")
        try:
            if url.startswith("data:"):
                m = re.match(r"data:([^;,]+)[^,]*,(.+)", url, re.S)
                raw = base64.b64decode(m.group(2)) if m else None
            elif url.startswith("http"):
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status != 200:
                            return None
                        raw = await resp.read()
            else:
                raw = None
            if not raw:
                return None
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{channel_name}_{idx}.{ext}"
            path = os.path.join(self._save_dir(), fname)
            with open(path, "wb") as f:
                f.write(raw)
            return path
        except Exception as e:
            logger.warning(f"media-luna 保存输出失败: {e}")
            return None

    async def _record(self, user_id, user_name, kind, channel, prompt, status, result="", task_id=None):
        async with self._io_lock:
            stats = load_json(
                STATS_FILE,
                {"total": {"image": 0, "video": 0}, "services": {}, "users": {}},
            )
            if status == "success":
                stats["total"].setdefault(kind, 0)
                stats["total"][kind] += 1
                svc = stats["services"].setdefault(channel, {"image": 0, "video": 0})
                svc[kind] += 1
                user = stats["users"].setdefault(user_id, {"name": user_name, "image": 0, "video": 0, "services": {}})
                user.setdefault("name", user_name)
                user[kind] += 1
                usvc = user["services"].setdefault(channel, {"image": 0, "video": 0})
                usvc[kind] += 1
                save_json(STATS_FILE, stats)
            tasks = load_json(TASKS_FILE, [])
            tasks.append({
                "id": task_id or uuid.uuid4().hex[:8],
                "ts": int(time.time()),
                "user_id": user_id,
                "user_name": user_name,
                "kind": kind,
                "channel": channel,
                "prompt": prompt,
                "status": status,
                "result": result,
            })
            save_json(TASKS_FILE, tasks[-MAX_TASKS:])

    """==================== 在线预设同步 ===================="""

    async def fetch_remote_templates(self, api_url: str) -> list[dict]:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise Exception(f"拉取在线预设失败 HTTP {resp.status}: {text[:200]}")
                data = json.loads(text)
        if isinstance(data, dict):
            code = data.get("code")
            if code is not None and code != 200:
                raise Exception(f"在线预设 API 错误: {data.get('message', code)}")
            return data.get("data") or []
        return data

    async def sync_presets(self, api_url: str = "") -> dict:
        api_url = api_url or str(self._cfg("preset_api_url", "https://prompt.vioaki.xyz/api/templates?per_page=-1"))
        result = {"added": 0, "updated": 0, "removed": 0, "errors": []}
        templates = await self.fetch_remote_templates(api_url)
        async with self._io_lock:
            presets = load_json(PRESETS_FILE, {})
            remote_ids = set()
            for t in templates:
                try:
                    tid = t.get("id")
                    if tid is None:
                        continue
                    remote_ids.add(tid)
                    # 自动生成关键词：type（txt2img→text2img）+ 模板 tags
                    raw_type = t.get("type")
                    type_tag = {
                        "txt2img": "text2img", "img2img": "img2img",
                        "text2video": "text2video", "img2video": "img2video",
                        "text2audio": "text2audio",
                    }.get(raw_type, raw_type)
                    tags = list(dict.fromkeys([x for x in [type_tag] + list(t.get("tags") or []) if x]))
                    refs = [
                        r.get("file_path") for r in sorted(
                            [r for r in (t.get("refs") or [])
                             if isinstance(r, dict) and not r.get("is_placeholder") and r.get("file_path")],
                            key=lambda x: x.get("position", 0),
                        )
                    ]
                    preset = {
                        "promptTemplate": t.get("prompt", ""),
                        "tags": tags,
                        "referenceImages": refs,
                        "parameterOverrides": {},
                        "source": "api",
                        "enabled": True,
                        "remoteId": tid,
                        "remoteUrl": api_url,
                        "thumbnail": t.get("thumbnail_path") or t.get("file_path") or "",
                    }
                    # 查找已有（同 remoteId + remoteUrl，或同名）
                    existing_key = None
                    for key, p in presets.items():
                        if p.get("remoteId") == tid and p.get("remoteUrl") == api_url:
                            existing_key = key
                            break
                    if existing_key is None:
                        for key, p in presets.items():
                            if key == t.get("title"):
                                existing_key = key
                                break
                    if existing_key:
                        old_enabled = presets[existing_key].get("enabled", True)
                        preset["enabled"] = old_enabled
                        presets[existing_key] = preset
                        result["updated"] += 1
                    else:
                        name = t.get("title") or f"preset-{tid}"
                        base, n = name, 2
                        while name in presets:
                            name = f"{base}-{n}"
                            n += 1
                        presets[name] = preset
                        result["added"] += 1
                except Exception as e:
                    result["errors"].append(str(e))
            if self._cfg("delete_removed", False):
                for key in list(presets.keys()):
                    p = presets[key]
                    if p.get("source") == "api" and p.get("remoteUrl") == api_url and p.get("remoteId") not in remote_ids:
                        del presets[key]
                        result["removed"] += 1
            save_json(PRESETS_FILE, presets)
        return result

    async def _auto_sync_loop(self):
        if not self._cfg("auto_sync", False):
            return
        interval = max(1, int(self._cfg("sync_interval", 60) or 60)) * 60
        await asyncio.sleep(5)
        while True:
            try:
                r = await self.sync_presets()
                logger.info(f"media-luna 在线预设同步完成: 新增 {r['added']} 更新 {r['updated']} 删除 {r['removed']}")
            except Exception as e:
                logger.warning(f"media-luna 在线预设同步失败: {e}")
            await asyncio.sleep(interval)

    """==================== 生命周期 ===================="""

    async def initialize(self):
        self._rebuild_channel_commands()
        self._register_page_apis()
        if self._cfg("auto_sync", False):
            self._sync_task = asyncio.create_task(self._auto_sync_loop())

    async def terminate(self):
        for old in list(self._channel_cmd_handlers):
            star_handlers_registry.remove(old)
        self._channel_cmd_handlers = []
        if self._channel_regex_handler is not None:
            star_handlers_registry.remove(self._channel_regex_handler)
            self._channel_regex_handler = None
        self._unregister_page_apis()
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None

    """==================== 插件页面（主 Dashboard 内嵌） ===================="""

    PAGE_API_PREFIX = "/media_luna/page"

    def _register_page_apis(self) -> None:
        register = self.context.register_web_api
        routes = [
            ("state", self._page_state, ["GET"], "Media Luna 状态"),
            ("connectors", self._page_connectors, ["GET"], "Media Luna 连接器"),
            ("channel/save", self._page_channel_save, ["POST"], "保存渠道"),
            ("channel/delete", self._page_channel_delete, ["POST"], "删除渠道"),
            ("preset/save", self._page_preset_save, ["POST"], "保存预设"),
            ("preset/delete", self._page_preset_delete, ["POST"], "删除预设"),
            ("preset/sync", self._page_preset_sync, ["POST"], "同步在线预设"),
            ("config", self._page_save_config, ["POST"], "保存配置"),
            ("task/<task_id>", self._page_task_detail, ["GET"], "任务详情"),
            ("upload", self._page_upload, ["POST"], "上传图片"),
            ("media/<filename>", self._page_media, ["GET"], "获取图片"),
        ]
        self._page_api_handlers = []
        for route, handler, methods, desc in routes:
            full = f"{self.PAGE_API_PREFIX}/{route}"
            register(full, handler, methods, desc)
            self._page_api_handlers.append((full, handler, methods, desc))
        logger.info("media-luna 插件页面 API 已注册（挂载于主 Dashboard /api/plug）")

    def _unregister_page_apis(self) -> None:
        try:
            apis = self.context.registered_web_apis
            for item in list(apis):
                if item in self._page_api_handlers:
                    apis.remove(item)
        except Exception:
            pass

    @staticmethod
    def _ok(data=None) -> dict:
        return {"status": "ok", "success": True, "data": data if data is not None else {}}

    @staticmethod
    def _err(message, **extra) -> dict:
        return {"status": "error", "success": False, "message": str(message), **extra}

    async def _request_body(self) -> dict:
        try:
            from quart import request
            data = await request.get_json(silent=True)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _page_state(self):
        stats = load_json(STATS_FILE, {"total": {"image": 0, "video": 0}, "services": {}, "users": {}})
        channels = load_json(CHANNELS_FILE, {})
        presets = load_json(PRESETS_FILE, {})
        try:
            with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8") as f:
                config_schema = json.load(f)
        except Exception:
            config_schema = {}
        config_items = []
        for k, v in self.config.items():
            schema_item = config_schema.get(k, {})
            item = {
                "key": k,
                "name": schema_item.get("name", k),
                "description": schema_item.get("description", ""),
                "value": v,
                "type": type(v).__name__,
            }
            if any(s in k.lower() for s in ("key", "token", "secret", "password")) and v:
                item["masked"] = True
                item["value"] = "****"
            config_items.append(item)
        return self._ok({
            "channels": channels,
            "presets": presets,
            "tasks": load_json(TASKS_FILE, [])[-50:],
            "stats": stats,
            "config": config_items,
            "configSchema": config_schema,
            "saveDir": self._save_dir(),
            "pluginDir": PLUGIN_DIR,
        })

    async def _page_connectors(self):
        return self._ok(CONNECTORS)

    async def _page_channel_save(self):
        body = await self._request_body()
        name = body.get("name", "").strip()
        data = body.get("data")
        if not name or not isinstance(data, dict):
            return self._err("invalid payload")
        if "connectorId" not in data or data["connectorId"] not in CONNECTORS:
            return self._err("invalid connector")
        data.setdefault("displayName", name)
        data.setdefault("enabled", True)
        data.setdefault("connectorConfig", {})
        data.setdefault("tags", [])
        async with self._io_lock:
            channels = load_json(CHANNELS_FILE, {})
            channels[name] = data
            save_json(CHANNELS_FILE, channels)
        self._rebuild_channel_commands()
        return self._ok()

    async def _page_channel_delete(self):
        body = await self._request_body()
        name = body.get("name", "")
        async with self._io_lock:
            channels = load_json(CHANNELS_FILE, {})
            if name in channels:
                del channels[name]
                save_json(CHANNELS_FILE, channels)
        self._rebuild_channel_commands()
        return self._ok()

    async def _page_preset_save(self):
        body = await self._request_body()
        name = body.get("name", "").strip()
        data = body.get("data")
        if not name or not isinstance(data, dict):
            return self._err("invalid payload")
        data.setdefault("source", "user")
        data.setdefault("enabled", True)
        data.setdefault("promptTemplate", "")
        data.setdefault("tags", [])
        data.setdefault("referenceImages", [])
        data.setdefault("parameterOverrides", {})
        async with self._io_lock:
            presets = load_json(PRESETS_FILE, {})
            old = presets.get(name)
            if old:
                data.setdefault("remoteId", old.get("remoteId"))
                data.setdefault("remoteUrl", old.get("remoteUrl"))
            presets[name] = data
            save_json(PRESETS_FILE, presets)
        return self._ok()

    async def _page_preset_delete(self):
        body = await self._request_body()
        name = body.get("name", "")
        async with self._io_lock:
            presets = load_json(PRESETS_FILE, {})
            if name in presets:
                del presets[name]
                save_json(PRESETS_FILE, presets)
        return self._ok()

    async def _page_preset_sync(self):
        try:
            return self._ok(await self.sync_presets())
        except Exception as e:
            return self._err(str(e))

    async def _page_save_config(self):
        updates = await self._request_body()
        if not isinstance(updates, dict):
            return self._err("invalid payload")
        for k, v in updates.items():
            if k in self.config:
                self.config[k] = v
        self._save_plugin_config()
        return self._ok()

    async def _page_upload(self):
        """上传图片（multipart 或 base64 JSON），保存到 media 目录"""
        raw = b""
        filename = "upload.png"
        try:
            from quart import request
            files = getattr(request, "files", None) or {}
            f = files.get("file") or files.get("image")
            if f is not None:
                raw = await f.read()
                filename = getattr(f, "filename", None) or "upload.png"
        except Exception:
            pass
        if not raw:
            body = await self._request_body()
            filename = body.get("filename", "upload.png")
            b64 = body.get("base64", "")
            try:
                raw = base64.b64decode(b64) if b64 else b""
            except Exception:
                return self._err("bad base64")
        if not raw:
            return self._err("no data")
        os.makedirs(MEDIA_DIR, exist_ok=True)
        safe = os.path.basename(filename) or "upload.png"
        name = f"{uuid.uuid4().hex[:8]}_{safe}"
        with open(os.path.join(MEDIA_DIR, name), "wb") as f:
            f.write(raw)
        return self._ok({"path": f"media/{name}"})

    async def _page_media(self, filename: str):
        safe = os.path.basename(filename or "")
        path = os.path.join(MEDIA_DIR, safe)
        if not os.path.exists(path):
            return self._err("not found")
        with open(path, "rb") as f:
            raw = f.read()
        return self._ok({"mime": guess_mime(safe), "base64": base64.b64encode(raw).decode()})

    async def _page_task_detail(self, task_id: str):
        target = None
        for r in reversed(load_json(TASKS_FILE, [])):
            if r.get("id") == task_id or r.get("id", "").startswith(task_id):
                target = r
                break
        if not target:
            return self._err("not found")
        return self._ok(target)

    """==================== 命令 ===================="""

    def _friendly_error(self, e: Exception, channel_name: str = "") -> str:
        """把底层异常转成简洁、可读的用户提示（完整堆栈只进日志）。"""
        text = str(e)
        low = text.lower()
        if any(k in text for k in ("safety", "blocked", "violation", "sensitive", "敏感", "安全", "非法内容")):
            return "❌ 生成失败：被内容安全策略拦截，请调整提示词或参考图后重试（可换用其他渠道/模型）"
        if "无可用渠道" in text or "no available channel" in low or "无可用模型" in text:
            model = ""
            chan = self.get_channel(channel_name) if channel_name else None
            if chan:
                model = str((chan.get("connectorConfig") or {}).get("model", "") or "")
            m = f"「{model}」" if model else ""
            return f"❌ 生成失败：模型{m}在当前服务商没有可用渠道（HTTP 503），请更换模型或稍后再试"
        if "expecting value" in low or "json.decoder" in low or "不是 json" in text or "响应不是 json" in low:
            return "❌ 生成失败：接口返回内容异常（不是标准 JSON），请检查渠道的请求地址/接口路径"
        if low.startswith("http "):
            code = text[5:].split(":", 1)[0].strip()
            return f"❌ 生成失败：接口返回 HTTP {code}，请检查渠道的密钥/地址/模型配置或稍后再试"
        if "timeout" in low or "超时" in text:
            return "❌ 生成失败：请求超时，请稍后重试或调大渠道超时时间"
        if "任务失败" in text:
            return "❌ 生成失败：服务端任务处理失败，请稍后重试"
        first = text.splitlines()[0].strip()
        if len(first) > 150:
            first = first[:150] + "…"
        return f"❌ 生成失败：{first}"

    async def _run_generate(self, event, args, kind, channel_override=None, prompt_override=None):
        """统一的生成入口。

        channel_override/prompt_override 用于渠道命令和重新生成（不再解析渠道名/预设）。
        输出为一条整合消息：任务ID（可选） + 图片/视频 + 用时（可选） + 模型（可选）。
        """
        if channel_override:
            channel_name = channel_override
            prompt, preset_name, preset = (prompt_override or "").strip(), None, None
        else:
            parts = (args or "").strip().split(maxsplit=1)
            if not parts:
                yield event.plain_result(f"用法: /{'video' if kind == 'video' else '<渠道>'} [预设] <提示词>")
                return
            channel_name = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            prompt, preset_name, preset = self._apply_preset(rest)
        chan = self.get_channel(channel_name)
        if not chan:
            yield event.plain_result(f"❌ 未找到渠道「{channel_name}」。可用: {', '.join(self.channel_names())}")
            return
        if not chan.get("enabled", True):
            yield event.plain_result(f"❌ 渠道「{channel_name}」已禁用")
            return
        if not prompt:
            yield event.plain_result("❌ 提示词为空")
            return
        # 去重：同一消息 ID + 同一渠道只生成一次，防止重复触发/重复上报
        now0 = time.time()
        msg_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        dedup_key = f"{event.get_sender_id()}:{msg_id}:{channel_name}"
        if msg_id:
            self._recent_generations = {
                k: ts for k, ts in self._recent_generations.items()
                if now0 - ts < 120
            }
            if dedup_key in self._recent_generations:
                return
            self._recent_generations[dedup_key] = now0
        task_id = uuid.uuid4().hex[:8]
        start = time.time()
        kind_label = {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, kind)
        preset_hint = f"（已套用预设 {preset_name}）" if preset_name else ""
        yield event.plain_result(f"⏳ 正在使用 {channel_name} 生成{kind_label}{preset_hint}...")
        try:
            files = await self._collect_reference_files(event, preset)
            assets = await self._generate(chan, prompt, files, kind)
        except Exception as e:
            logger.error(f"media-luna generate error: {e}\n{traceback.format_exc()}")
            await self._record(
                event.get_sender_id(), event.get_sender_name(), kind, channel_name,
                prompt, "failed", task_id=task_id,
            )
            yield event.plain_result(self._friendly_error(e, channel_name))
            return
        elapsed = time.time() - start
        model_name = (
            (chan.get("connectorConfig") or {}).get("model")
            or CONNECTORS.get(chan.get("connectorId", ""), {}).get("name", "-")
        )
        result = event.make_result()
        # 消息前缀：任务 ID
        if self._cfg("output_show_task_id", True):
            result.message(f"任务ID: {task_id}")
        # 图片/视频
        first_result = ""
        idx = 0
        for asset in assets:
            if asset.get("kind") == "text":
                if asset.get("content"):
                    result.message(str(asset["content"])[:500])
                continue
            path = await self._save_output(asset, channel_name, idx)
            idx += 1
            if path:
                if asset.get("kind") == "video":
                    result.chain.append(Video.fromFileSystem(path))
                elif asset.get("kind") == "audio":
                    result.chain.append(Record.fromFileSystem(path))
                else:
                    result.chain.append(Image.fromFileSystem(path))
                if not first_result:
                    first_result = path
            elif asset.get("url") and asset["url"].startswith("http"):
                if asset.get("kind") == "video":
                    result.chain.append(Video.fromURL(asset["url"]))
                elif asset.get("kind") == "audio":
                    result.chain.append(Record.fromURL(asset["url"]))
                else:
                    result.url_image(asset["url"])
                if not first_result:
                    first_result = asset["url"]
        # 消息后缀：用时 + 模型
        trailer = []
        if self._cfg("output_show_elapsed", True):
            trailer.append(f"⏱ 用时 {elapsed:.1f}s")
        if self._cfg("output_show_model", True):
            trailer.append(f"模型: {model_name}")
        if trailer:
            result.message(" | ".join(trailer))
        if not result.chain:
            await self._record(
                event.get_sender_id(), event.get_sender_name(), kind, channel_name,
                prompt, "failed", task_id=task_id,
            )
            yield event.plain_result("❌ 生成服务未返回可发送的内容")
            return
        await self._record(
            event.get_sender_id(), event.get_sender_name(), kind, channel_name,
            prompt, "success", first_result, task_id=task_id,
        )
        yield result

    def _rebuild_channel_commands(self) -> None:
        """将每个启用的渠道注册为真实指令（如 /nano <提示词>），并清理旧的动态指令"""
        for old in list(self._channel_cmd_handlers):
            star_handlers_registry.remove(old)
        self._channel_cmd_handlers = []
        if self._channel_regex_handler is not None:
            star_handlers_registry.remove(self._channel_regex_handler)
            self._channel_regex_handler = None
        channels = load_json(CHANNELS_FILE, {})
        for name, ch in channels.items():
            if not ch.get("enabled", True):
                continue
            cid = ch.get("connectorId", "")
            if cid not in CONNECTORS:
                continue
            if cid in AUDIO_CONNECTOR_IDS and cid not in IMAGE_CONNECTOR_IDS and cid not in VIDEO_CONNECTOR_IDS:
                kind = "audio"
            elif cid in VIDEO_CONNECTOR_IDS and cid not in IMAGE_CONNECTOR_IDS:
                kind = "video"
            else:
                kind = "image"

            def make_handler(cmd_name=name, cmd_kind=kind):
                async def _channel_cmd(event):
                    raw = (event.get_message_str() or "").strip()
                    if not raw.startswith("/"):
                        # 免唤醒入口（正则）会处理非斜杠消息，这里只负责 /渠道 形式，避免重复触发
                        return
                    text = raw[1:]
                    if text == cmd_name:
                        rest = ""
                    elif text.startswith(cmd_name + " "):
                        rest = text[len(cmd_name) + 1:].strip()
                    else:
                        return
                    async for r in self._run_generate(
                        event, "", cmd_kind, channel_override=cmd_name, prompt_override=rest
                    ):
                        yield r

                _channel_cmd.__name__ = f"channelcmd_{cmd_name}"
                return _channel_cmd

            handler = make_handler()
            md = StarHandlerMetadata(
                event_type=EventType.AdapterMessageEvent,
                handler_full_name=f"{self.__module__}__channelcmd_{name}",
                handler_name=f"channelcmd_{name}",
                handler_module_path=self.__module__,
                handler=handler,
                event_filters=[],
                desc=f"渠道指令: {name}",
            )
            md.event_filters.append(CommandFilter(name, None, md))
            star_handlers_registry.append(md)
            self._channel_cmd_handlers.append(md)

        # 免唤醒前缀的渠道指令（正则匹配，任意群聊/私聊均可直接触发）
        enabled_names = [
            name for name, ch in channels.items()
            if ch.get("enabled", True) and ch.get("connectorId") in CONNECTORS
        ]
        if enabled_names:
            pattern = r"^(?:" + "|".join(
                re.escape(n) for n in sorted(enabled_names, key=len, reverse=True)
            ) + r")(?=\s|$)"

            def make_regex_handler():
                async def _channel_regex(event):
                    text = event.get_message_str().strip()
                    if text.startswith("/"):
                        return
                    first = text.split(maxsplit=1)[0] if text else ""
                    chan = self.get_channel(first)
                    if not chan or not chan.get("enabled", True):
                        return
                    cid = chan.get("connectorId", "")
                    if cid not in CONNECTORS:
                        return
                    if cid in AUDIO_CONNECTOR_IDS and cid not in IMAGE_CONNECTOR_IDS and cid not in VIDEO_CONNECTOR_IDS:
                        kind = "audio"
                    elif cid in VIDEO_CONNECTOR_IDS and cid not in IMAGE_CONNECTOR_IDS:
                        kind = "video"
                    else:
                        kind = "image"
                    rest = text[len(first):].strip()
                    async for r in self._run_generate(
                        event, "", kind, channel_override=first, prompt_override=rest
                    ):
                        yield r

                _channel_regex.__name__ = "channel_regex"
                return _channel_regex

            handler = make_regex_handler()
            md = StarHandlerMetadata(
                event_type=EventType.AdapterMessageEvent,
                handler_full_name=f"{self.__module__}__channel_regex",
                handler_name="channel_regex",
                handler_module_path=self.__module__,
                handler=handler,
                event_filters=[RegexFilter(pattern)],
                desc="渠道指令（免唤醒前缀）",
            )
            star_handlers_registry.append(md)
            self._channel_regex_handler = md

        logger.info(
            f"media-luna 已注册 {len(self._channel_cmd_handlers)} 个渠道指令"
            f"{' + 免唤醒入口' if self._channel_regex_handler else ''}"
        )

    @filter.command("redraw", alias={"重新生成"})
    async def redraw(self, event: AstrMessageEvent, task_id: str):
        target = None
        for r in reversed(load_json(TASKS_FILE, [])):
            if r.get("id") == task_id or r.get("id", "").startswith(task_id):
                target = r
                break
        if not target:
            yield event.plain_result(f"❌ 未找到任务 {task_id}")
            return
        chan = self.get_channel(target.get("channel", ""))
        if not chan or not chan.get("enabled", True):
            yield event.plain_result(f"❌ 原渠道「{target.get('channel')}」不存在或已禁用")
            return
        yield event.plain_result(f"🔄 正在重新生成任务 {target.get('id')}...")
        async for r in self._run_generate(
            event, "", target.get("kind", "image"),
            channel_override=target.get("channel", ""),
            prompt_override=target.get("prompt", ""),
        ):
            yield r

    @filter.command("video", alias={"视频"})
    async def video(self, event: AstrMessageEvent, args: GreedyStr):
        async for r in self._run_generate(event, args, "video"):
            yield r

    @filter.command("channels", alias={"渠道"})
    async def channels(self, event: AstrMessageEvent):
        channels = load_json(CHANNELS_FILE, {})
        if not channels:
            yield event.plain_result("还没有配置渠道")
            return
        lines = ["📡 可用渠道:"]
        for name, ch in channels.items():
            mark = "" if ch.get("enabled", True) else "（已禁用）"
            cid = ch.get("connectorId", "?")
            model = (ch.get("connectorConfig") or {}).get("model") or "-"
            lines.append(f"- {name} ({CONNECTORS.get(cid, {}).get('name', cid)}) 模型: {model} {mark}")
        yield event.plain_result("\n".join(lines))

    @filter.command("models", alias={"模型"})
    async def models(self, event: AstrMessageEvent):
        await self.channels(event)

    @filter.command("preset", alias={"预设"})
    async def preset(self, event: AstrMessageEvent, args: GreedyStr):
        parts = (args or "").strip().split()
        presets = load_json(PRESETS_FILE, {})
        if not parts or parts[0] == "list":
            if not presets:
                yield event.plain_result("还没有预设。使用 /preset sync 拉取在线预设，或 /preset add <触发词> <提示词> 添加。")
                return
            lines = ["📋 预设列表（触发词）:"]
            for name, p in presets.items():
                mark = "" if p.get("enabled", True) else "（禁用）"
                src = "在线" if p.get("source") == "api" else "本地"
                tags = ",".join(p.get("tags", [])[:3]) or "-"
                lines.append(f"- {name} [{src}] 关键词: {tags} {mark}")
            yield event.plain_result("\n".join(lines[:30]))
            return
        cmd = parts[0]
        if cmd == "add":
            if len(parts) < 3:
                yield event.plain_result("用法: /preset add <触发词> <提示词>")
                return
            name = parts[1]
            prompt_text = " ".join(parts[2:])
            presets[name] = {
                "promptTemplate": prompt_text,
                "tags": [],
                "referenceImages": [],
                "parameterOverrides": {},
                "source": "user",
                "enabled": True,
                "thumbnail": "",
            }
            save_json(PRESETS_FILE, presets)
            yield event.plain_result(f"✅ 已添加预设「{name}」（可到 WebUI 补充关键词/参考图/缩略图）")
        elif cmd == "del":
            if len(parts) < 2:
                yield event.plain_result("用法: /preset del <触发词>")
                return
            name = parts[1]
            if name in presets:
                del presets[name]
                save_json(PRESETS_FILE, presets)
                yield event.plain_result(f"✅ 已删除预设「{name}」")
            else:
                yield event.plain_result(f"❌ 未找到预设「{name}」")
        elif cmd == "sync":
            yield event.plain_result("🔄 正在同步在线预设...")
            try:
                r = await self.sync_presets()
                msg = f"✅ 同步完成: 新增 {r['added']}，更新 {r['updated']}，删除 {r['removed']}"
                if r["errors"]:
                    msg += f"\n⚠️ {len(r['errors'])} 个模板失败"
                yield event.plain_result(msg)
            except Exception as e:
                yield event.plain_result(f"❌ 同步失败: {e}")
        else:
            yield event.plain_result("用法: /preset list | sync | add <触发词> <提示词> | del <触发词>")

    @filter.command("tasks", alias={"任务"})
    async def tasks(self, event: AstrMessageEvent, count: int = 10):
        count = max(1, min(count, 50))
        records = load_json(TASKS_FILE, [])[-count:]
        if not records:
            yield event.plain_result("还没有任务记录")
            return
        lines = ["🗂 最近任务:"]
        for r in reversed(records):
            ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
            mark = "✅" if r.get("status") == "success" else "❌"
            lines.append(f"{mark} [{r.get('id')}] {ts} {r.get('user_name', '?')} {r.get('kind')}/{r.get('channel')}: {r.get('prompt', '')[:30]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("taskinfo")
    async def taskinfo(self, event: AstrMessageEvent, task_id: str):
        target = None
        for r in reversed(load_json(TASKS_FILE, [])):
            if r.get("id") == task_id or r.get("id", "").startswith(task_id):
                target = r
                break
        if not target:
            yield event.plain_result(f"❌ 未找到任务 {task_id}")
            return
        lines = [
            f"任务 ID: {target.get('id')}",
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(target.get('ts', 0)))}",
            f"用户: {target.get('user_name', '?')}",
            f"类型: {target.get('kind')} / {target.get('channel')}",
            f"状态: {target.get('status')}",
            f"提示词: {target.get('prompt', '')}",
        ]
        if target.get("result"):
            lines.append(f"结果: {target['result']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("stats", alias={"统计"})
    async def stats(self, event: AstrMessageEvent):
        stats_data = load_json(STATS_FILE, {"total": {"image": 0, "video": 0}, "services": {}, "users": {}})
        total = stats_data.get("total", {})
        lines = [
            "📊 生成统计",
            f"图片: {total.get('image', 0)} 次 | 视频: {total.get('video', 0)} 次 | 音频: {total.get('audio', 0)} 次",
            "按服务:",
        ]
        services = stats_data.get("services", {})
        if not services:
            lines.append("  （暂无）")
        for name, svc in services.items():
            lines.append(f"  - {name}: 图片 {svc.get('image', 0)} / 视频 {svc.get('video', 0)} / 音频 {svc.get('audio', 0)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("mystats", alias={"我的统计"})
    async def mystats(self, event: AstrMessageEvent):
        uid = event.get_sender_id()
        stats_data = load_json(STATS_FILE, {"total": {"image": 0, "video": 0}, "services": {}, "users": {}})
        user = stats_data.get("users", {}).get(uid)
        if not user:
            yield event.plain_result("你还没有生成记录")
            return
        lines = [
            f"📊 {user.get('name', uid)} 的统计",
            f"图片: {user.get('image', 0)} 次 | 视频: {user.get('video', 0)} 次 | 音频: {user.get('audio', 0)} 次",
        ]
        services = user.get("services", {})
        if services:
            lines.append("按服务:")
            for name, svc in services.items():
                lines.append(f"  - {name}: 图片 {svc.get('image', 0)} / 视频 {svc.get('video', 0)} / 音频 {svc.get('audio', 0)}")
        yield event.plain_result("\n".join(lines))
