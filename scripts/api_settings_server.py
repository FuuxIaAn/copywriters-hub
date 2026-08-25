# -*- coding: utf-8 -*-
"""
API 设置中心
============
集中管理所有外部 API 配置：主 LLM、硅基流动 ASR、ModelScope TTS。
提供读取、保存、测试以及自动从浏览器抓取 ModelScope Token 的能力。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests

# ---------------------------------------------------------------- 工具函数


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _mask(s: str, head: int = 6, tail: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= head + tail + 3:
        return s[:2] + "***" + s[-2:] if len(s) > 4 else "***"
    return s[:head] + "..." + s[-tail:]


# ---------------------------------------------------------------- 配置路径

# 打包成 exe 后 BASE_DIR 是只读解压目录，config.json 的修改版保存在可写数据目录
_DATA_DIR = os.environ.get("WB_DATA_DIR") or ""


def _config_path() -> str:
    """读取配置：优先数据目录覆盖版，其次打包内/项目根版本。"""
    if _DATA_DIR:
        override = os.path.join(_DATA_DIR, "config.json")
        if os.path.exists(override):
            return override
    return os.path.join(_ROOT, "config.json")


def _load_merged_config() -> dict:
    """读取完整配置：用户目录只作为非空覆盖层。"""
    base = _read_json(os.path.join(_ROOT, "config.json"), {})
    override = _read_json(_config_path(), {})
    if not isinstance(base, dict):
        base = {}
    if not isinstance(override, dict):
        return base

    def merge(dst, src):
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                merge(dst[key], value)
            elif value not in (None, ""):
                dst[key] = value
        return dst
    return merge(base, override)


def _config_save_path() -> str:
    """保存配置：打包模式写到数据目录覆盖版，开发模式直接写项目根。"""
    if _DATA_DIR:
        return os.path.join(_DATA_DIR, "config.json")
    return os.path.join(_ROOT, "config.json")


def _asr_settings_path(output_dir: str) -> str:
    return os.path.join(output_dir, "extract", "asr_settings.json")


def _tts_settings_path(output_dir: str) -> str:
    return os.path.join(output_dir, "tts", "settings.json")


# ---------------------------------------------------------------- 读取 / 保存


def get_all_settings(output_dir: str) -> dict:
    """返回当前所有 API 设置（不含明文密钥，只返回掩码）。"""
    config = _load_merged_config()
    api = config.get("api", {})
    llm_key = os.environ.get("WB_LLM_API_KEY", "").strip() or api.get("api_key", "")

    asr = _read_json(_asr_settings_path(output_dir), {})
    tts = _read_json(_tts_settings_path(output_dir), {})

    return {
        "ok": True,
        "llm": {
            "base_url": api.get("base_url", ""),
            "api_key_masked": _mask(llm_key),
            "model": api.get("model", ""),
        },
        "asr": {
            "has_key": bool(asr.get("api_key", "").strip()),
            "key_masked": _mask(asr.get("api_key", "")),
        },
        "tts": {
            "has_token": bool(tts.get("token", "").strip()),
            "token_masked": _mask(tts.get("token", "")),
            "backend": (tts.get("backend") or "modelscope") if (tts.get("backend") in ("modelscope", "local")) else "modelscope",
        },
    }


def save_llm_settings(base_url: str, api_key: str, model: str) -> dict:
    """保存主 LLM 配置（保留文件中其他字段；打包模式写数据目录覆盖版）。"""
    save_path = _config_save_path()
    config = _load_merged_config()
    if not config:
        return {"ok": False, "error": "读取 config.json 失败"}
    config.setdefault("api", {})
    if base_url is not None and base_url.strip():
        config["api"]["base_url"] = base_url.strip().rstrip("/")
    elif not config["api"].get("base_url"):
        return {"ok": False, "error": "Base URL 不能为空"}
    if api_key is not None:
        key = api_key.strip()
        if key:
            config["api"]["api_key"] = key
    if model is not None and model.strip():
        config["api"]["model"] = model.strip()
    elif not config["api"].get("model"):
        return {"ok": False, "error": "模型名不能为空"}
    _write_json(save_path, config)
    return {"ok": True}


def save_asr_settings(output_dir: str, api_key: str) -> dict:
    """保存硅基流动 ASR Key。"""
    import asr_server
    return asr_server.save_asr_settings(output_dir, api_key)


def save_tts_settings(output_dir: str, token: str, backend: str = "") -> dict:
    """保存 ModelScope TTS Token 与推理后端。"""
    import tts_server
    return tts_server.save_settings(output_dir, token, backend=backend)


# ---------------------------------------------------------------- 测试


def test_llm(config: dict) -> dict:
    """测试主 LLM 是否可用。"""
    try:
        from openai import OpenAI
        api = config.get("api", {})
        if not api.get("base_url"):
            return {"ok": False, "error": "未配置 LLM Base URL"}
        if not api.get("api_key") and not os.environ.get("WB_LLM_API_KEY", "").strip():
            return {"ok": False, "error": "未配置 LLM API Key"}
        if not api.get("model"):
            return {"ok": False, "error": "未配置 LLM 模型名"}
        client = OpenAI(base_url=api.get("base_url"), api_key=api.get("api_key"), timeout=30, max_retries=2)
        last_error = None
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=api.get("model", "deepseek-chat"),
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=5,
                )
                return {"ok": True, "model": resp.model}
            except Exception as error:  # noqa: BLE001
                last_error = error
                text = str(error).lower()
                if not any(k in text for k in ("ssl", "handshake", "timeout", "connection", "reset", "eof")):
                    break
                if attempt < 2:
                    import time
                    time.sleep(2 * (attempt + 1))
        text = str(last_error or "")
        if any(k in text.lower() for k in ("ssl", "handshake", "timeout", "connection", "reset", "eof")):
            return {"ok": False, "error": "LLM 网络连接超时，请检查网络/代理后重试"}
        return {"ok": False, "error": f"LLM 连接失败：{text[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"LLM 连接失败：{str(e)[:200]}"}


def test_asr(output_dir: str) -> dict:
    """测试硅基流动 ASR Key 是否有效（用 1 秒静音探测）。"""
    import asr_server
    key = asr_server._load_key(output_dir)
    if not key:
        return {"ok": False, "error": "未配置硅基流动 API Key"}
    try:
        import wave
        import io
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        buf.seek(0)
        files = {"file": ("test.wav", buf, "audio/wav")}
        data = {"model": "FunAudioLLM/SenseVoiceSmall", "language": "auto"}
        r = requests.post(asr_server.ASR_API, headers={"Authorization": f"Bearer {key}"}, files=files, data=data, timeout=20)
        if r.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"ASR 测试失败：{e}"}


def test_tts(output_dir: str) -> dict:
    """测试 ModelScope TTS Token 是否有效。"""
    import tts_server
    token = tts_server._load_token(output_dir)
    if not token:
        return {"ok": False, "error": "未配置 ModelScope Token"}
    # Client 构造会拉取创空间 API 配置，可能长时间阻塞——复用 tts_server 的带超时连接
    try:
        client = tts_server._connect_client(token)
        if not client:
            return {"ok": False, "error": "ModelScope 连接超时，请稍后重试"}
        # 只调用 view_api 做鉴权探测，不真正生成
        info = client.view_api(return_format="dict") or {}
        endpoints = sorted((info.get("named_endpoints") or {}).keys())
        return {"ok": True, "endpoints": endpoints[:5]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"ModelScope 连接失败：{e}"}


# ---------------------------------------------------------------- 自动抓取 ModelScope Token


def fetch_modelscope_token_from_browser() -> dict:
    """
    尝试从用户已登录的浏览器中自动获取 ModelScope 访问令牌。
    策略：
      1) 用 browser_cookie3 读取 Chrome/Edge/Firefox 中 modelscope.cn 的 cookie；
      2) 带 cookie 访问个人中心相关 API，提取 ms- 开头的 SDK Token；
      3) 若失败，尝试用 Playwright 启动系统浏览器并抓取页面文本。
    返回：{ok, token?, error?}
    """
    # 先尝试 cookie + API
    result = _fetch_via_cookie_api()
    if result.get("ok"):
        return result

    # 兜底：Playwright 页面抓取
    return _fetch_via_playwright(fallback_error=result.get("error"))


def _get_modelscope_cookies() -> dict:
    """读取本机浏览器中 modelscope.cn 的 cookie。"""
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name="modelscope.cn")
        cookies = {c.name: c.value for c in cj}
        if cookies:
            return cookies
    except Exception:  # noqa: BLE001
        pass
    for loader in (browser_cookie3.edge, browser_cookie3.firefox):
        try:
            cj = loader(domain_name="modelscope.cn")
            cookies = {c.name: c.value for c in cj}
            if cookies:
                return cookies
        except Exception:  # noqa: BLE001
            pass
    return {}


def _fetch_via_cookie_api() -> dict:
    cookies = _get_modelscope_cookies()
    if not cookies:
        return {"ok": False, "error": "未找到 modelscope.cn 的浏览器登录态，请先在 Chrome/Edge 登录"}

    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Referer": "https://www.modelscope.cn/my/myaccesstoken",
    })

    # 官方获取令牌列表的端点
    url = "https://www.modelscope.cn/api/v1/users/tokens/list"
    try:
        r = session.get(url, timeout=15)
        data = r.json() if r.status_code == 200 else {}
        tokens = data.get("Data", {}).get("SdkTokens", [])
        for t in tokens:
            token = (t.get("SdkToken") or "").strip()
            if token.startswith("ms-"):
                return {"ok": True, "token": token, "source": "cookie_api", "name": t.get("SdkTokenName", "")}
        # 兜底：从原始响应文本再扫一遍
        m = re.search(r'"SdkToken"\s*:\s*"(ms-[a-zA-Z0-9_\-]+)"', r.text)
        if m:
            return {"ok": True, "token": m.group(1), "source": "cookie_api_fallback"}
        return {"ok": False, "error": "未在令牌列表中找到 ms- 格式的 SDK Token"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Cookie 方式未找到 Token：{e}"}


def _fetch_via_playwright(fallback_error: str = "") -> dict:
    """使用 Playwright 启动系统 Chrome 并访问令牌页面抓取。"""
    node_exe = os.environ.get("NODE_EXE") or shutil.which("node")
    if not node_exe:
        return {"ok": False, "error": f"未找到 node 环境，无法启动浏览器自动化（{fallback_error}）"}

    # 构建临时 JS 脚本
    fd, script_path = tempfile.mkstemp(suffix=".js")
    try:
        js = r'''
const { chromium } = require('playwright');
const os = require('os');
const path = require('path');
(async () => {
  let ctx;
  try {
    // 优先复用用户已登录的真实 Chrome/Edge 配置（登录态存在 localStorage，普通 headless 拿不到）。
    // 候选：Chrome User Data / Edge User Data / 系统默认 profile。
    const candidates = [
      process.env.LOCALAPPDATA + '\\Google\\Chrome\\User Data',
      process.env.LOCALAPPDATA + '\\Microsoft\\Edge\\User Data',
    ];
    const channel = candidates[0] && require('fs').existsSync(candidates[0]) ? 'chrome' : 'msedge';
    const userDataDir = candidates.find(d => require('fs').existsSync(d));
    const launchOpts = {
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
    };
    if (userDataDir) {
      launchOpts.channel = channel;
      ctx = await Promise.race([
        chromium.launchPersistentContext(userDataDir, launchOpts),
        new Promise((_, rej) => setTimeout(() => rej(new Error('启动浏览器超时（Chrome 可能正被占用）')), 30000))
      ]);
    } else {
      ctx = await chromium.launch({ channel: channel, headless: true, args: ['--no-sandbox'] });
    }
    const page = ctx.pages()[0] || await ctx.newPage();
    await page.goto('https://www.modelscope.cn/my/myaccesstoken', { waitUntil: 'domcontentloaded', timeout: 30000 });
    // 等待页面异步渲染出 SDK Token（ms- 开头长串），最多 15s
    try {
      await page.waitForFunction(
        () => /\bms-[a-zA-Z0-9_\-]{24,}\b/.test(document.body.innerText),
        { timeout: 15000, polling: 500 }
      );
    } catch (_) {}
    const text = await page.evaluate(() => document.body.innerText);
    const url = page.url();
    // 提取 SDK Token：排除页面路由/导航文本（ms-page-、ms-studios 等）
    const candidates2 = (text.match(/ms-[a-zA-Z0-9_\-]{24,}/g) || []);
    const bad = /ms-(page|studios|sdk|catalog|model|datasets|spaces|users|issues|docs|help|api|dashboard|web|token|list|git|oss|auth|signin|signup|home|profile|setting)/i;
    const filtered = candidates2.filter(t => !bad.test(t));
    if (filtered.length) {
      console.log(JSON.stringify({ ok: true, token: filtered[0], source: 'playwright_profile' }));
    } else {
      const loggedIn = /(新建令牌|创建令牌|SDK Token|access token|退出登录|登出|Logout)/i.test(text);
      console.log(JSON.stringify({
        ok: false,
        error: loggedIn
          ? '已登录但未在令牌页找到 ms- 开头的 SDK Token，请先在 modelscope.cn 个人中心「创建令牌」'
          : '未检测到 modelscope.cn 登录态（需先在 Chrome/Edge 登录）；请登录后重试，或关闭已打开的浏览器避免配置占用',
        snippet: (text || '').slice(0, 200),
        url: url
      }));
    }
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: e.message }));
  } finally {
    if (ctx) { try { await ctx.close(); } catch (e) {} }
  }
})();
'''
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)

        env = os.environ.copy()
        # 让 require 能找到 workspace 下的 playwright
        env["NODE_PATH"] = "C:\\Users\\linsh\\.workbuddy\\binaries\\node\\workspace\\node_modules"
        try:
            result = subprocess.run(
                [node_exe, script_path],
                capture_output=True, text=True, timeout=55, env=env,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False,
                    "error": "自动获取超时（浏览器配置可能正被占用）。可关闭已打开的浏览器后重试，或到 modelscope.cn 个人中心「访问令牌」复制 SDK Token 后手动粘贴。"}
        # 从 stdout 找 JSON 行
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        err = (result.stderr or result.stdout or fallback_error or "Playwright 未返回有效结果").strip()
        return {"ok": False, "error": err[:500]}
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass
