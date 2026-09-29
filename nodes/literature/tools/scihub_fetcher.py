"""全文获取模块

优先级：
1. Unpaywall OA PDF（合法免费）
2. Sci-Hub（学术用途）
3. arXiv（预印本）
4. Browser 抓摘要（兜底）

注意：如果学校 VPN/代理可用，配置 HTTP_PROXY 环境变量
"""

import os
import re
import time
import random
import logging
import json
import shutil
from pathlib import Path
from urllib.parse import quote

import httpx

from core import paths as _paths

logger = logging.getLogger(__name__)


def _browser_executable() -> str:
    """选择 Playwright 使用的浏览器可执行文件。"""
    configured = os.environ.get("HARNESS_BROWSER_EXECUTABLE", "").strip()
    if configured and Path(configured).is_file():
        return configured
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def paper_cache_dir(output_dir: str | None = None) -> str:
    """跨项目论文资产根目录（真相源 core.paths）：正式归档按 DOI 写入 `<doi>/{index,paper,figure}/`。

    v0.9 之前这里硬编码 `~/survey-harness/papers`，跑到框架 root 之外，备份 / 迁移
    都会漏掉。现在默认落 `$HARNESS_FRAMEWORK_HOME/literature/papers/` —— 故意是
    跨项目共享缓存而不是 run/project 级，否则同一篇 PDF 每个 run 都要重下一遍。

    显式传 output_dir 仍然生效（expanduser 后原样使用）。
    """
    if output_dir:
        return os.path.expanduser(str(output_dir))
    d = _paths.literature_papers_dir()
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def legacy_paper_path(filename: str) -> str | None:
    """旧 `~/survey-harness/papers/<filename>`，存在才返回。

    读回退用：v0.9 之前下载的 PDF 还在旧目录里，命中就直接复用，避免重下；新文件
    一律写新目录；正式归档不会再把 PDF 和图片平铺在 papers 根目录。
    """
    legacy = _paths.legacy_literature_dir("papers")
    if legacy is None:
        return None
    hit = legacy / filename
    return str(hit) if hit.exists() else None


# 代理配置（从环境变量读取）
def _get_proxy() -> str:
    """获取明确配置的 HTTP 代理地址。

    自动探测只在显式开启且明确识别为 WSL 时运行，避免把 Linux 上的
    本机服务误当成代理。
    """
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if proxy:
        return proxy
    if os.environ.get("HARNESS_AUTO_DETECT_WSL_PROXY") != "1":
        return ""
    try:
        version = open("/proc/version", encoding="utf-8").read().lower()
        if "microsoft" not in version and "wsl" not in version:
            return ""
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("nameserver"):
                    continue
                fields = line.split()
                if len(fields) < 2:
                    continue
                win_ip = fields[1]
                if win_ip.startswith(("127.", "0.", "::1")):
                    continue
                for port in [7897, 7890, 1080, 10808, 10809]:
                    try:
                        import socket
                        with socket.create_connection((win_ip, port), timeout=1):
                            return f"http://{win_ip}:{port}"
                    except OSError:
                        continue
    except (OSError, ValueError, IndexError):
        logger.debug("WSL proxy auto-detection failed", exc_info=True)
    return ""


# 全局代理地址
HTTP_PROXY = _get_proxy()
if HTTP_PROXY:
    print(f"[全文获取] 检测到代理: {HTTP_PROXY}")

# Unpaywall 邮箱（必须）
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "").strip()

# Sci-Hub 镜像（静态兜底列表；运行时优先使用 lovescihub.wordpress.com 动态列表，
# 与 `sci-hub` PyPI 工具同源 —— 镜像域名经常更换，硬编码列表会过期）
SCI_HUB_MIRRORS = [
    "https://sci-hub.ren",
    "https://sci-hub.ee",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.wf",
    "https://sci-hub.pub",
    "https://sci-hub.tf",
]

# 动态镜像列表缓存（进程内，10 分钟刷新一次，避免每次下载都打 lovescihub）
_SCIHUB_MIRROR_CACHE: list[str] = []
_SCIHUB_MIRROR_CACHE_AT = 0.0
_SCIHUB_MIRROR_TTL = 600.0


def _discover_scihub_mirrors() -> list[str]:
    """获取可用 Sci-Hub 镜像列表。

    参照 `sci-hub` 工具（https://github.com/suqingdong/scihub）的 check_host：
    解析 lovescihub.wordpress.com（每 5 分钟更新一次可用域名），失败回退静态列表。
    环境变量 HARNESS_SCIHUB_MIRROR（逗号分隔）可显式指定镜像并排在最前。
    """
    global _SCIHUB_MIRROR_CACHE, _SCIHUB_MIRROR_CACHE_AT
    now = time.time()
    if _SCIHUB_MIRROR_CACHE and now - _SCIHUB_MIRROR_CACHE_AT < _SCIHUB_MIRROR_TTL:
        return list(_SCIHUB_MIRROR_CACHE)

    mirrors: list[str] = []
    override = os.environ.get("HARNESS_SCIHUB_MIRROR", "").strip()
    if override:
        mirrors = [m.strip().rstrip("/") for m in override.split(",") if m.strip()]
    try:
        resp = httpx.get(
            "https://lovescihub.wordpress.com/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36"},
            timeout=15, follow_redirects=True,
        )
        if resp.status_code == 200:
            # 只解析正文区（entry-content），并只接受 sci-hub 域名，
            # 避免把博客侧栏的 wp.me / github.com 等链接当成镜像
            m = re.search(r'<div class="entry-content">(.*?)</div>',
                          resp.text, re.IGNORECASE | re.S)
            text = re.sub(r"<[^>]+>", " ", m.group(1) if m else resp.text)
            for host in re.findall(r"https?://[a-zA-Z0-9.\-]+", text):
                host = host.rstrip("/")
                if "sci-hub" not in host.lower() and "scihub" not in host.lower():
                    continue
                if host not in mirrors:
                    mirrors.append(host)
    except Exception as exc:
        logger.debug("Sci-Hub mirror discovery failed: %s", exc)
    if not mirrors:
        mirrors = list(SCI_HUB_MIRRORS)
    _SCIHUB_MIRROR_CACHE, _SCIHUB_MIRROR_CACHE_AT = mirrors, now
    return list(mirrors)


# ---- Sci-Hub 请求节流 -------------------------------------------------
# 官方 `sci-hub` 工具在每篇论文之间随机睡 3~8s；请求越密越容易被限流。
# 基础间隔可用环境变量调整：
#   HARNESS_SCIHUB_MIN_DELAY / HARNESS_SCIHUB_MAX_DELAY（秒，默认 2 / 5）
# 另外带自适应乘数：连续被限流后自动加倍拉长，成功后缓慢回落，
# 让批量跑在撞上限流墙时自动变得"更客气"。
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


_SCIHUB_MIN_DELAY = _env_float("HARNESS_SCIHUB_MIN_DELAY", 2.0)
_SCIHUB_MAX_DELAY = _env_float("HARNESS_SCIHUB_MAX_DELAY", 5.0)
_SCIHUB_THROTTLE = 1.0  # 自适应节流乘数，1.0~8.0


def _scihub_delay() -> float:
    """单次请求间隔（秒）：配置区间 × 自适应乘数，随后缓慢回落。"""
    global _SCIHUB_THROTTLE
    delay = random.uniform(_SCIHUB_MIN_DELAY, _SCIHUB_MAX_DELAY) * _SCIHUB_THROTTLE
    _SCIHUB_THROTTLE = max(1.0, _SCIHUB_THROTTLE * 0.95)
    return delay


def _scihub_bump_throttle() -> None:
    """一轮扫描全部被限流后调用：加倍拉长后续请求间隔。"""
    global _SCIHUB_THROTTLE
    _SCIHUB_THROTTLE = min(8.0, _SCIHUB_THROTTLE * 2.0)


def _scihub_ease_throttle() -> None:
    """单次下载成功后调用：放松节流（减半，直至回到 1.0）。"""
    global _SCIHUB_THROTTLE
    _SCIHUB_THROTTLE = max(1.0, _SCIHUB_THROTTLE * 0.5)

# Elsevier API Key（从 .env 读取）
def _get_elsevier_key() -> str:
    """读取 Elsevier API Key"""
    # 先检查当前目录 .env
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                if line.startswith("ELSEVIER_API_KEY="):
                    return line.strip().split("=", 1)[1]
    # 再检查 ~/.hermes/.env
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("ELSEVIER_API_KEY="):
                    return line.strip().split("=", 1)[1]
    return ""


def fetch_full_text_via_elsevier(doi: str, output_dir: str | None = None) -> dict:
    """通过 Elsevier API 获取 PDF 全文
    
    注意：个人 Academic API Key 可能只返回首页预览（1页），
    代码会自动检查页数验证完整性。
    
    Returns:
        {"pdf_path": "", "text": "", "source": "elsevier|none"}
    """
    result = {"pdf_path": "", "text": "", "source": "none"}
    if not doi:
        return result
    
    api_key = _get_elsevier_key()
    if not api_key or len(api_key) < 10:
        return result
    
    output_dir = paper_cache_dir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    safe_name = _cache_filename(doi)
    output_path = os.path.join(output_dir, safe_name)
    
    # 已存在且完整
    if _valid_pdf_cache(output_path):
        try:
            import fitz
            doc = fitz.open(output_path)
            pages = len(doc)
            doc.close()
            if pages >= 2:
                text = _extract_text(output_path)
                if len(text) > 500:
                    return {"pdf_path": output_path, "text": text, "source": "elsevier_cached"}
        except ImportError:
            pass
    
    try:
        url = f"https://api.elsevier.com/content/article/doi/{doi}"
        headers = {
            "X-ELS-APIKey": api_key,
            "Accept": "application/pdf",
        }
        resp = httpx.get(url, headers=headers, timeout=30)
        
        if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
            with open(output_path, "wb") as f:
                f.write(resp.content)
            # 验证页数（>=2 才算完整全文）
            try:
                import fitz
                doc = fitz.open(output_path)
                pages = len(doc)
                doc.close()
                if pages < 2:
                    print(f"  [Elsevier] ⚠️ 只有 {pages} 页预览，删除")
                    os.remove(output_path)
                    return result
            except ImportError:
                pass
            text = _extract_text(output_path)
            if len(text) > 500:
                print(f"  [Elsevier] ✅ {doi[:40]}... ({pages if 'pages' in dir() else '?'} 页, {len(text)} 字符)")
                return {"pdf_path": output_path, "text": text, "source": "elsevier"}
            else:
                os.remove(output_path)
    except Exception as e:
        print(f"  [Elsevier] ❌ 异常: {e}")
    
    return result


def fetch_full_text_via_unpaywall(doi: str, output_dir: str | None = None,
                                   timeout: int = 30) -> dict:
    """通过 Unpaywall 获取 OA 全文"""
    result = {"pdf_path": "", "text": "", "source": "none", "oa_status": "",
              "is_oa": False, "oa_check": "unknown", "oa_url": "", "oa_landing_url": "",
              "oa_pdf_urls": [], "oa_landing_urls": []}
    if not doi or not UNPAYWALL_EMAIL:
        return result
    
    doi = doi.strip().replace("https://doi.org/", "").replace("http://doi.org/", "")
    output_dir = paper_cache_dir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    safe_name = _cache_filename(doi)
    output_path = os.path.join(output_dir, safe_name)
    
    if _valid_pdf_cache(output_path):
        text = _extract_text(output_path)
        if len(text) > 500:
            return {"pdf_path": output_path, "text": text, "source": "unpaywall_cached", "oa_status": "cached"}
    
    try:
        url = f"https://api.unpaywall.org/v2/{doi}?email={UNPAYWALL_EMAIL}"
        # 使用代理
        client_kwargs = {}
        if HTTP_PROXY:
            client_kwargs["proxy"] = HTTP_PROXY
        
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url, timeout=15)
        
        if resp.status_code != 200:
            return result
        
        data = resp.json()
        result["oa_status"] = data.get("oa_status", "")
        result["is_oa"] = data.get("is_oa", False)

        if not data.get("is_oa"):
            return result

        # 记住 OA URL 供后续策略使用
        best = data.get("best_oa_location", {}) or {}
        if best.get("url_for_pdf"):
            result["oa_url"] = best["url_for_pdf"]
        if best.get("url_for_landing_page"):
            result["oa_landing_url"] = best["url_for_landing_page"]

        oa_locations = data.get("oa_locations", []) or []
        # 保留所有 OA 候选。best location 的直链可能过期或被 WAF 拦截，
        # 其他 location（尤其是机构仓储）仍可能可用。
        pdf_urls = []
        landing_urls = []
        for loc in ([best] if best else []) + oa_locations:
            pdf = loc.get("url_for_pdf", "")
            landing = loc.get("url_for_landing_page", "")
            if pdf and pdf not in pdf_urls:
                pdf_urls.append(pdf)
            if landing and landing not in landing_urls:
                landing_urls.append(landing)
        result["oa_pdf_urls"] = pdf_urls
        result["oa_landing_urls"] = landing_urls
        if not result["oa_landing_url"] and landing_urls:
            result["oa_landing_url"] = landing_urls[0]
        pdf_url = pdf_urls[0] if pdf_urls else None
        
        if pdf_url:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/pdf,*/*",
                }
                with httpx.Client(**client_kwargs) as client:
                    pdf_resp = client.get(pdf_url, headers=headers, follow_redirects=True, timeout=60)
                
                if pdf_resp.status_code == 200 and len(pdf_resp.content) > 5000:
                    if pdf_resp.content[:5] == b"%PDF-":
                        with open(output_path, "wb") as f:
                            f.write(pdf_resp.content)
                        text = _extract_text(output_path)
                        if len(text) > 500:
                            print(f"  [Unpaywall] ✅ OA PDF: {doi[:40]}...")
                            return {"pdf_path": output_path, "text": text,
                                    "source": "unpaywall", "oa_status": result["oa_status"],
                                    "is_oa": True, "oa_check": "oa",
                                    "oa_url": result.get("oa_url", ""),
                                    "oa_landing_url": result.get("oa_landing_url", "")}
                        else:
                            os.remove(output_path)
            except Exception as e:
                print(f"  [Unpaywall] PDF 下载失败: {e}")
    except Exception as e:
        print(f"  [Unpaywall] 查询失败: {e}")
    
    return result


def _failure_cache_path(output_dir: str) -> str:
    return os.path.join(output_dir, ".fulltext_failures.json")


def _provider_blocked(provider: str, key: str, output_dir: str) -> bool:
    """负缓存/熔断：短时间内不要重复打一个已连续失败的提供方。"""
    try:
        with open(_failure_cache_path(output_dir), encoding="utf-8") as f:
            entries = json.load(f)
        entry = entries.get(f"{provider}:{key}", {})
        return float(entry.get("retry_after", 0)) > time.time()
    except (OSError, ValueError, TypeError):
        return False


def _record_provider_failure(provider: str, key: str, output_dir: str) -> None:
    path = _failure_cache_path(output_dir)
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        entries = {}
    cache_key = f"{provider}:{key}"
    entry = entries.get(cache_key, {})
    failures = int(entry.get("failures", 0)) + 1
    # 只有连续失败达到阈值才熔断，避免一次临时网络错误永久影响。
    if failures >= 3:
        entry["retry_after"] = time.time() + min(3600, 60 * (2 ** min(failures - 3, 5)))
    entry["failures"] = failures
    entry["last_failed_at"] = time.time()
    entries[cache_key] = entry
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.warning("cannot persist full-text failure cache: %s", path)


def _clear_provider_failure(provider: str, key: str, output_dir: str) -> None:
    path = _failure_cache_path(output_dir)
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
        if entries.pop(f"{provider}:{key}", None) is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
    except (OSError, ValueError):
        return


def fetch_pdf_from_scihub(doi: str, output_dir: str | None = None,
                          timeout: int = 30) -> str:
    """通过 DOI 从 Sci-Hub 下载 PDF。

    实现参照 `sci-hub` PyPI 工具（https://github.com/suqingdong/scihub）：
      - 镜像列表优先从 lovescihub.wordpress.com 动态获取（每 5 分钟更新），
        失败回退静态列表；可用 HARNESS_SCIHUB_MIRROR 环境变量显式指定；
      - 随机轮换镜像起点、请求间隔随机 sleep（默认 2~5s，官方工具 3~8s 同量级；
        HARNESS_SCIHUB_MIN_DELAY / HARNESS_SCIHUB_MAX_DELAY 可调），规避镜像反爬限流
        （短时间高频请求会返回 403 / 验证码页，这是 Sci-Hub 下载失败的主因）；
        连续被限流后间隔自动加倍（封顶 8x），成功后缓慢回落；
      - 页面含搜索表单时按表单 POST（request=<doi>），否则 GET /{doi}、/doi/{doi}；
      - 命中『文章未找到』页立即返回，不再扫其余镜像浪费时间。

    注意：仅按 DOI 做负缓存，不做提供方级熔断 —— 镜像限流是常态，
    提供方级熔断会让批量下载看起来像 Sci-Hub 完全不可用。
    """
    if not doi or os.environ.get("HARNESS_ENABLE_SCIHUB", "1") != "1":
        return ""

    output_dir = paper_cache_dir(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    doi = doi.strip().replace("https://doi.org/", "").replace("http://doi.org/", "")
    safe_name = _cache_filename(doi)
    output_path = os.path.join(output_dir, safe_name)
    timeout = min(timeout, int(os.environ.get("HARNESS_SCIHUB_TIMEOUT_SECONDS", "10")))
    max_sweeps = max(1, int(os.environ.get("HARNESS_SCIHUB_MAX_SWEEPS", "1")))
    mirror_limit = max(1, int(os.environ.get("HARNESS_SCIHUB_MIRROR_LIMIT", "2")))

    if _valid_pdf_cache(output_path):
        return output_path
    if _provider_blocked("scihub", doi, output_dir):
        logger.info("Sci-Hub circuit open for %s", doi)
        return ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,*/*",
    }
    pdf_headers = {**headers, "Accept": "application/pdf,application/octet-stream,*/*"}

    mirrors = _discover_scihub_mirrors()
    if not mirrors:
        mirrors = list(SCI_HUB_MIRRORS)
    # 随机轮换起点：避免每次都先打同一个镜像而被限流
    offset = random.randrange(len(mirrors))
    mirrors = mirrors[offset:] + mirrors[:offset]
    mirrors = mirrors[:mirror_limit]

    client_kwargs = {"timeout": timeout, "follow_redirects": True,
                     "verify": True, "http1": True, "http2": False}
    if HTTP_PROXY:
        client_kwargs["proxy"] = HTTP_PROXY

    try:
        with httpx.Client(**client_kwargs) as client:
            # 默认做有界扫描：Sci-Hub 可用时仍会命中；被挑战/限流时不能拖住
            # 整个 literature_index 产出。需要深抓全文时可调高 env 上限。
            for sweep in range(max_sweeps):
                challenged_hits = 0
                attempts = 0
                for mirror in mirrors:
                    for url in (f"{mirror}/{quote(doi)}", f"{mirror}/doi/{doi}"):
                        try:
                            resp = client.get(url, headers=headers)
                            attempts += 1  # 只统计拿到响应的请求
                            if resp.status_code != 200:
                                if _scihub_page_challenged("", resp.status_code):
                                    challenged_hits += 1
                                continue
                            if _scihub_article_missing(resp.text):
                                logger.info("Sci-Hub: article not found on %s for %s",
                                            mirror, doi)
                                return ""
                            if _scihub_page_challenged(resp.text, resp.status_code):
                                challenged_hits += 1
                                continue
                            pdf_url = _extract_pdf_url(resp.text, mirror)
                            if not pdf_url and "<form" in resp.text.lower():
                                # 返回的是带搜索表单的首页：按官方工具方式 POST request=<doi>
                                action = re.search(
                                    r'<form[^>]*action=["\']([^"\']*)["\']',
                                    resp.text, re.IGNORECASE)
                                post_url = mirror
                                if action and action.group(1) not in ("", "/"):
                                    a = action.group(1)
                                    post_url = (a if a.startswith("http")
                                                else mirror.rstrip("/") + "/" + a.lstrip("/"))
                                try:
                                    r2 = client.post(
                                        post_url,
                                        data={"sci-hub-plugin-check": "", "request": doi},
                                        headers=headers)
                                    if (r2.status_code == 200
                                            and not _scihub_article_missing(r2.text)):
                                        pdf_url = _extract_pdf_url(r2.text, mirror)
                                except Exception as exc:
                                    logger.debug("Sci-Hub POST search failed (%s): %s",
                                                 mirror, exc)
                            if not pdf_url:
                                continue

                            # 下载 PDF（最多 3 次，指数退避）
                            for attempt in range(3):
                                try:
                                    pdf_resp = client.get(pdf_url, headers=pdf_headers,
                                                           timeout=60)
                                    if (pdf_resp.status_code == 200
                                            and len(pdf_resp.content) > 5000
                                            and pdf_resp.content[:5] == b"%PDF-"):
                                        with open(output_path, "wb") as f:
                                            f.write(pdf_resp.content)
                                        print(f"  [Sci-Hub] ✅ {mirror}")
                                        _clear_provider_failure("scihub", doi, output_dir)
                                        _scihub_ease_throttle()
                                        return output_path
                                except Exception as exc:
                                    logger.warning("Sci-Hub PDF response failed (%s): %s",
                                                   mirror, exc)
                                    if attempt < 2:
                                        time.sleep(1 + attempt)
                        except Exception as exc:
                            logger.warning("Sci-Hub mirror request failed (%s): %s",
                                           mirror, exc)
                        time.sleep(_scihub_delay())
                # 全部请求都是验证码页 → 限流，退避后重扫
                if challenged_hits >= attempts and attempts > 0 and sweep < max_sweeps - 1:
                    _scihub_bump_throttle()
                    wait = 10 * (sweep + 1)
                    logger.info("Sci-Hub mirrors rate-limited (%d/%d challenged); "
                                "backoff %ds and resweep (throttle=%.1fx)",
                                challenged_hits, attempts, wait, _SCIHUB_THROTTLE)
                    time.sleep(wait)
                    continue
                break
    except Exception as exc:
        logger.warning("Sci-Hub client failed: %s", exc)

    # 负缓存：同一 DOI 短时间内不再重试
    _record_provider_failure("scihub", doi, output_dir)
    return ""


def fetch_pdf_from_publisher_direct(doi: str, output_dir: str | None = None,
                                     timeout: int = 20) -> str:
    """通过出版商 HTML 页提取 citation_pdf_url 后直接下载 PDF.

    适用于无 WAF 保护的出版商（Copernicus 等小型/OA 出版商）。
    对返回短响应 (<5KB) 或 403 的出版商（被 WAF 拦截）快速跳过。

    Returns:
        PDF 路径（成功）或 ""（失败）
    """
    if not doi:
        return ""

    doi = doi.strip().replace("https://doi.org/", "").replace("http://doi.org/", "")
    output_dir = paper_cache_dir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    safe_name = _cache_filename(doi)
    output_path = os.path.join(output_dir, safe_name)

    if _valid_pdf_cache(output_path):
        return output_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    urls_to_try = [
        f"https://doi.org/{doi}",
    ]

    for url in urls_to_try:
        try:
            # httpx 对某些服务器 hang，用 requests 替代
            # 强制 IPv4（Copernicus 等服务器的 IPv6 连接会 hang）
            import requests as _req
            import socket as _sk
            _old_getaddrinfo = _sk.getaddrinfo
            def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
                return _old_getaddrinfo(host, port, _sk.AF_INET, type, proto, flags)
            _sk.getaddrinfo = _ipv4_only
            _req_kw = {"timeout": timeout, "headers": headers, "verify": True}
            if HTTP_PROXY:
                _req_kw["proxies"] = {"http": HTTP_PROXY, "https": HTTP_PROXY}
            resp = _req.get(url, **_req_kw)
            
            # 短响应或 403 → 被 WAF 拦截，跳过
            if resp.status_code == 403 or len(resp.content) < 5000:
                continue

            html = resp.text
            pdf_url = _extract_pdf_url(html, str(resp.url))
            if not pdf_url:
                continue

            # 下载 PDF（用 requests 替代 httpx）
            _pdf_kw = {"timeout": min(timeout, 15), "headers": {**headers, "Accept": "application/pdf,*/*"}, "verify": True}
            if HTTP_PROXY:
                _pdf_kw["proxies"] = {"http": HTTP_PROXY, "https": HTTP_PROXY}
            pdf_resp = _req.get(pdf_url, **_pdf_kw)
            if pdf_resp.status_code == 200 and len(pdf_resp.content) > 5000:
                    if pdf_resp.content[:5] == b"%PDF-":
                        with open(output_path, "wb") as f:
                            f.write(pdf_resp.content)
                        print(f"  [Publisher] ✅ {url[:60]}...")
                        _sk.getaddrinfo = _old_getaddrinfo
                        return output_path
        except Exception as exc:
            logger.warning("publisher direct request failed: %s", exc)
            _sk.getaddrinfo = _old_getaddrinfo
            continue

    _sk.getaddrinfo = _old_getaddrinfo
    return ""


def fetch_pdf_via_browser(doi: str, output_dir: str | None = None,
                           timeout: int = 45, oa_url: str = "") -> str:
    """用 Playwright 浏览器导航出版商页面，提取 PDF 链接并下载.

    适用场景:
      - 无 WAF 或弱 WAF 保护的出版商
      - 需要执行 JavaScript 才能显示 PDF 链接的页面
      - 配置校园网代理后可访问订阅期刊

    策略:
      1. 等待页面加载完成（过 JS 挑战）
      2. 查找 citation_pdf_url meta 标签
      3. 查找页面中的 PDF 下载链接 / pdfft 链接
      4. 如果找到，导航到 PDF 链接并下载

    注意:
      - 比 httpx 慢很多（每篇 10-45s），仅在 httpx 失败时尝试
      - Elsevier/ACS 等强 WAF 出版商即使浏览器也可能被拦截
      - 配置 HTTP_PROXY 环境变量可用校园网代理访问

    Returns:
        PDF 路径（成功）或 ""（失败）
    """
    if not doi:
        return ""

    doi_clean = doi.strip().replace("https://doi.org/", "").replace("http://doi.org/", "")
    output_dir = paper_cache_dir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    safe_name = _cache_filename(doi_clean)
    output_path = os.path.join(output_dir, safe_name)

    if _valid_pdf_cache(output_path):
        return output_path

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""

    proxy_settings = {}
    if HTTP_PROXY:
        proxy_settings["server"] = HTTP_PROXY

    try:
        with sync_playwright() as pw:
            launch_kwargs = {
                "headless": True,
                "timeout": 20000,
                "args": [
                    # 新版无头模式 (Chrome 112+)，更难检测
                    "--headless=new",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                    "--start-maximized",
                    "--disable-gpu",
                ],
            }
            executable = _browser_executable()
            if executable:
                launch_kwargs["executable_path"] = executable
            browser = pw.chromium.launch(
                **launch_kwargs,
                proxy=proxy_settings if proxy_settings else None,
            )
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                permissions=["geolocation"],
            )
            page = context.new_page()
            page.set_default_timeout(timeout * 1000)

            # 强反自动化检测
            page.add_init_script("""
                // 隐藏 webdriver
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                // 伪装 plugins (headless 为空)
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                // 伪装 languages
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                // 伪装 chrome 对象
                window.chrome = {
                    runtime: { onConnect: { addListener: () => {} } },
                    loadTimes: () => {},
                    csi: () => {},
                    app: {},
                };
                // 覆盖权限查询
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (p) => (
                    p.name === 'notifications' ? Promise.resolve({ state: 'denied' }) : originalQuery(p)
                );
            """)

            # 如果 Unpaywall 给的是 PDF 直链，先在浏览器上下文中直接请求。
            # 这一步能复用浏览器 UA/cookies，避开 httpx 被 MDPI CDN 拒绝，
            # 也避免 Chromium PDF viewer 不暴露下载响应。
            if oa_url and ("/pdf" in oa_url.lower() or oa_url.lower().split("?")[0].endswith(".pdf")):
                try:
                    req_headers = {
                        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/120.0.0.0 Safari/537.36"),
                        "Accept": "application/pdf,application/octet-stream,*/*",
                    }
                    direct = context.request.get(oa_url, headers=req_headers,
                                                 max_redirects=10,
                                                 timeout=timeout * 1000)
                    body = direct.body()
                    if (direct.status == 200 and len(body) > 5000
                            and body[:5] == b"%PDF-"):
                        with open(output_path, "wb") as f:
                            f.write(body)
                        print(f"  [Browser] ✅ {doi_clean[:40]}... ({len(body)//1024}KB)")
                        browser.close()
                        return output_path
                except Exception:
                    logger.debug("browser direct OA PDF request failed", exc_info=True)

            # 如果 Unpaywall 已经返回了 OA URL，直接用这个 URL
            url = oa_url if oa_url else f"https://doi.org/{doi_clean}"
            pdf_url = None

            # 导航到 DOI/OA 页面
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                # 如果是 OA URL 且 403（Cloudflare CDN 挑战），回退到 DOI 页面
                if not resp or resp.status == 403 and oa_url:
                    url = f"https://doi.org/{doi_clean}"
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                # 等待额外时间让 JS 加载
                page.wait_for_timeout(3000)
            except Exception as exc:
                logger.warning("browser landing page failed for %s: %s", doi_clean, exc)
                browser.close()
                return ""

            # 检查是否被拦截（短错误页面）
            body_text = page.inner_text("body")
            if "problem providing the content" in body_text.lower():
                browser.close()
                return ""

            # 1. 尝试 citation_pdf_url meta 标签
            try:
                meta = page.query_selector('meta[name="citation_pdf_url"]')
                if meta:
                    pdf_url = meta.get_attribute("content")
            except Exception as exc:
                logger.warning("browser citation_pdf_url lookup failed: %s", exc)

            # 2. 查找 pdfft / pdf 下载链接
            if not pdf_url:
                try:
                    candidates = page.eval_on_selector_all(
                        'a[href*="pdfft"], a[href*="/pdf"], a[download]',
                        "els => els.map(el => el.href || el.getAttribute('data-url') || '')",
                    )
                    for c in candidates:
                        if c and not c.startswith("javascript"):
                            pdf_url = c
                            break
                except Exception:
                    logger.warning("browser PDF link lookup failed", exc_info=True)

            # 3. 查找"Download PDF"按钮
            if not pdf_url:
                try:
                    btns = page.eval_on_selector_all(
                        'a:has-text("PDF"), a:has-text("Download"), button:has-text("PDF")',
                        "els => els.map(el => el.href || el.getAttribute('data-target') || '')",
                    )
                    for b in btns:
                        if b and b.startswith("http"):
                            pdf_url = b
                            break
                except Exception:
                    logger.warning("browser download button lookup failed", exc_info=True)

            if not pdf_url:
                browser.close()
                return ""

            # 首选浏览器上下文请求：复用页面 cookies/headers，但不经过
            # Chromium 内置 PDF viewer，能稳定拿到真实 PDF 响应。
            try:
                req_headers = {
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0.0.0 Safari/537.36"),
                    "Accept": "application/pdf,application/octet-stream,*/*",
                }
                req_resp = context.request.get(pdf_url, headers=req_headers,
                                               max_redirects=10,
                                               timeout=timeout * 1000)
                body = req_resp.body()
                content_type = (req_resp.headers.get("content-type", "") or "").lower()
                if (req_resp.status == 200 and len(body) > 5000
                        and (body[:5] == b"%PDF-" or "application/pdf" in content_type)):
                    if body[:5] != b"%PDF-":
                        # Do not save an HTML error page merely because its
                        # server advertised a broad content type.
                        raise ValueError("response is not a PDF")
                    with open(output_path, "wb") as f:
                        f.write(body)
                    print(f"  [Browser] ✅ {doi_clean[:40]}... ({len(body)//1024}KB)")
                    browser.close()
                    return output_path
            except Exception:
                logger.debug("browser context PDF request failed", exc_info=True)

            # 兜底：用 Playwright 事件监听捕获 PDF 响应。
            pdf_content = None
            def _on_response(resp):
                nonlocal pdf_content
                ct = resp.headers.get('content-type', '')
                if pdf_url in resp.url and ('application/pdf' in ct or 'octet-stream' in ct):
                    try:
                        pdf_content = resp.body()
                    except Exception as exc:
                        logger.warning("browser response body read failed: %s", exc)
            page.on('response', _on_response)

            resp = page.goto(pdf_url, wait_until="domcontentloaded",
                             timeout=timeout * 1000)
            page.wait_for_timeout(3000)

            if pdf_content and len(pdf_content) > 5000 and pdf_content[:5] == b"%PDF-":
                with open(output_path, "wb") as f:
                    f.write(pdf_content)
                print(f"  [Browser] ✅ {doi_clean[:40]}... ({len(pdf_content)//1024}KB)")
                browser.close()
                return output_path

            # 兜底：从页面 url 参数或 iframe 中提取
            try:
                iframe = page.query_selector('iframe[src*="pdf"]')
                if iframe:
                    pdf_url2 = iframe.get_attribute("src")
                    if pdf_url2 and not pdf_url2.startswith("http"):
                        pdf_url2 = f"https:{pdf_url2}" if pdf_url2.startswith("//") else pdf_url2
                    if pdf_url2:
                        pdf_content = None
                        def _on_resp2(r):
                            nonlocal pdf_content
                            ct = r.headers.get('content-type', '')
                            if 'application/pdf' in ct or 'octet-stream' in ct:
                                try:
                                    pdf_content = r.body()
                                except Exception:
                                    logger.warning("browser response body read failed", exc_info=True)
                        page.on('response', _on_resp2)
                        page.goto(pdf_url2, wait_until="domcontentloaded", timeout=timeout * 1000)
                        page.wait_for_timeout(2000)
                        if pdf_content and len(pdf_content) > 5000 and pdf_content[:5] == b"%PDF-":
                            with open(output_path, "wb") as f:
                                f.write(pdf_content)
                            print(f"  [Browser] ✅ {doi_clean[:40]}... ({len(pdf_content)//1024}KB)")
                            browser.close()
                            return output_path
            except Exception as exc:
                logger.warning("browser iframe PDF capture failed: %s", exc)

            browser.close()
    except Exception as exc:
        logger.warning("browser full-text fetch failed for %s: %s", doi_clean, exc)

    return ""


def _clean_pdf_url(raw: str, mirror: str) -> str:
    """清洗 Sci-Hub 页面里提取的 PDF URL。

    - 去掉转义斜杠（页面里常见 `https:\\/\\/host\\/...` 的 onclick 写法）
    - 去掉 #fragment（如 #view=FitH；fragment 不会发给服务器，且部分客户端会报错）
    - 补全 // 协议相对 / 站内相对路径
    """
    if not raw:
        return ""
    url = raw.strip().replace("\\/", "/")
    url = url.split("#", 1)[0]
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = mirror.rstrip("/") + url
    return url


def _scihub_article_missing(html: str) -> bool:
    """识别 Sci-Hub 的『文章未找到』页。

    命中即认为该 DOI 不在 Sci-Hub 库中，直接跳过其余镜像，避免无谓请求。
    只认明确的 not-found 文本标记；不能用 title 判断 —— 反爬页
    （如 "Sci-Hub: проверка на робота"）title 也是 bare "Sci-Hub"，
    误判会让正常文章被提前放弃。
    """
    if not html:
        return False
    text = html.lower()
    if ("article not found" in text or "статья не найдена" in text
            or "не найдена" in text or "文章未找到" in text
            or "id=\"not_found\"" in text or "class=\"notfound" in text):
        return True
    return False


def _scihub_page_challenged(html: str, status: int = 200) -> bool:
    """识别 Sci-Hub 镜像的反爬/验证码页（限流信号）。

    短时间高频请求后，镜像会返回 Turnstile 验证页 / "Checking your browser" /
    "проверка на робота" / 403。这类页面不等于『文章未找到』——
    全部镜像都命中时应当退避重试，而不是放弃该 DOI。
    """
    if status == 403:
        return True
    if not html:
        return False
    text = html.lower()
    if any(k in text for k in ("turnstile", "challenges.cloudflare",
                               "checking your browser", "проверка на робота",
                               "cf-chl", "cf_chl", "captcha")):
        return True
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.S)
    if m:
        title = m.group(1).strip().lower()
        if "verification" in title and "sci-hub" in title:
            return True
    return False


def _extract_pdf_url(html: str, mirror: str) -> str:
    """从 Sci-Hub HTML 中提取 PDF URL。

    按可靠性排序：
      1. <embed src>（Sci-Hub 文章页的标准容器，官方工具取 #pdf 的 src）
      2. <meta name="citation_pdf_url">
      3. <object data="...pdf">
      4. <iframe src>（排除验证码/广告域名）
      5. onclick="location.href='...'" 下载按钮（含 \\/ 转义斜杠）
      6. <a href="...pdf">
      7. location.href= 仅当指向 .pdf
    """
    candidates: list[str] = []
    # 1. embed（Sci-Hub 文章页的标准容器，官方工具取 #pdf 的 src）
    for m in re.finditer(r'<embed[^>]*>', html, re.IGNORECASE):
        tag = m.group(0)
        sm = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if sm:
            candidates.append(sm.group(1))
    # 2. meta citation_pdf_url
    for m in re.finditer(r'<meta[^>]+citation_pdf_url[^>]*>', html, re.IGNORECASE):
        sm = re.search(r'\bcontent\s*=\s*["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        if sm:
            candidates.append(sm.group(1))
    # 3. object data
    for m in re.finditer(r'<object[^>]*>', html, re.IGNORECASE):
        sm = re.search(r'\bdata\s*=\s*["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        if sm:
            candidates.append(sm.group(1))
    # 4. iframe src
    for m in re.finditer(r'<iframe[^>]*>', html, re.IGNORECASE):
        sm = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        if sm:
            candidates.append(sm.group(1))
    # 5. onclick location.href 下载按钮
    for m in re.finditer(
            r'onclick\s*=\s*["\'][^"\']*location\.href\s*=\s*["\']([^"\']+)["\']',
            html, re.IGNORECASE):
        candidates.append(m.group(1))
    # 6. a href 指向 .pdf
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\']',
                         html, re.IGNORECASE):
        candidates.append(m.group(1))
    # 7. 通用 location.href（仅接受 .pdf 结尾，避免验证码页误提取）
    for m in re.finditer(r'location\.href\s*=\s*["\']([^"\']+)["\']',
                         html, re.IGNORECASE):
        low = m.group(1).lower()
        if low.endswith(".pdf") or ".pdf?" in low:
            candidates.append(m.group(1))

    for raw in candidates:
        url = _clean_pdf_url(raw, mirror)
        if not url or not url.startswith(("http://", "https://")):
            continue
        if any(k in url.lower() for k in ("turnstile", "captcha",
                                          "challenges.cloudflare")):
            continue
        return url
    return ""


def _extract_text(pdf_path: str) -> str:
    """从 PDF 提取文本；任何失败都返回空字符串。"""
    if not pdf_path or not os.path.isfile(pdf_path):
        return ""
    try:
        with open(pdf_path, "rb") as f:
            if f.read(5) != b"%PDF-":
                return ""
    except OSError:
        return ""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception:
        logger.warning("PDF text extraction failed for %s", pdf_path, exc_info=True)
        return ""


def _valid_pdf_cache(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        if os.path.getsize(path) <= 5000:
            return False
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def fetch_arxiv_pdf(arxiv_id: str, output_dir: str | None = None) -> str:
    """下载 arXiv PDF"""
    if not arxiv_id:
        return ""
    
    output_dir = paper_cache_dir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    safe_name = f"arxiv_{arxiv_id.replace('/', '_')}.pdf"
    output_path = os.path.join(output_dir, safe_name)
    
    if _valid_pdf_cache(output_path):
        return output_path
    
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    try:
        client_kwargs = {"timeout": 60, "follow_redirects": True, "http1": True, "http2": False}
        if HTTP_PROXY:
            client_kwargs["proxy"] = HTTP_PROXY
        
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url)
            if resp.status_code == 200 and len(resp.content) > 5000 and resp.content[:5] == b"%PDF-":
                with open(output_path, "wb") as f:
                    f.write(resp.content)
                return output_path
    except Exception as exc:
        logger.warning("arXiv PDF request failed for %s: %s", arxiv_id, exc)
    return ""


# ============================================================================
# 出版商感知的全文获取策略
# ============================================================================

# Edge CDP 真实浏览器下载（跨 WSL 调用 Windows Edge）
def _fetch_pdf_via_edge_cdp(doi: str, output_dir: str | None = None,
                             oa_url: str = "") -> str:
    """通过 Windows Edge CDP 用真实浏览器下载 PDF（解决 MDPI Akamai 限制）."""
    try:
        from chrome_cdp_fetcher import fetch_via_cdp
        return fetch_via_cdp(doi, output_dir, oa_url=oa_url)
    except ImportError:
        logger.info("Edge CDP unavailable: dependency not installed")
        return ""
    except Exception as exc:
        logger.warning("Edge CDP fetch failed for %s: %s", doi, exc)
        return ""


def _cache_filename(doi: str, ext: str = ".pdf") -> str:
    """生成带出版商前缀的缓存文件名，如 'MDPI_10.3390_jmse11030549.pdf'."""
    safe_doi = re.sub(r'[^\w\-\.]', '_', doi.strip()
                      .replace("https://doi.org/", "").replace("http://doi.org/", ""))
    publisher, _ = _classify_publisher(doi)
    pub_prefix = f"{publisher}_" if publisher and publisher != "Unknown" else ""
    return f"{pub_prefix}{safe_doi}{ext}"


def _fetch_pdf_via_edge_cdp_landing(doi: str, output_dir: str | None = None) -> str:
    """通过 Edge CDP 导航到文章页面(DOI)，提取 citation_pdf_url 后下载 PDF。

    适用场景:
        - Elsevier/Cell.com OA: PDF URL 触发 Chromium PDF viewer → 改为走文章页面提取
    """
    try:
        from chrome_cdp_fetcher import fetch_via_cdp_landing
        return fetch_via_cdp_landing(doi, output_dir)
    except ImportError:
        logger.info("Edge CDP landing unavailable: dependency not installed")
        return ""
    except Exception as exc:
        logger.warning("Edge CDP landing failed for %s: %s", doi, exc)
        return ""

# 出版商 DOI 前缀映射
PUBLISHER_BY_DOI = {
    "10.1016":  "Elsevier",
    "10.1021":  "ACS",
    "10.1007":  "Springer",
    "10.1002":  "Wiley",
    "10.1080":  "TaylorFrancis",
    "10.1088":  "IOP",
    "10.1038":  "Nature",
    "10.1039":  "RSC",
    "10.1029":  "AGU",
    "10.1098":  "RoyalSociety",
    "10.1111":  "Wiley",
    "10.1126":  "Science",
    "10.1101":  "bioRxiv",
    "10.1109":  "IEEE",
    "10.1145":  "ACM",
    "10.1175":  "AMS",
    "10.1186":  "Springer",
    "10.1242":  "CompanyOfBiologists",
    "10.1364":  "Optica",
    "10.1371":  "PLOS",
    "10.15252": "EMBO",
    "10.2134":  "ASA",
    "10.2147":  "DovePress",
    "10.2166":  "IWA",
    "10.2174":  "Bentham",
    "10.2196":  "JMIR",
    "10.33540": "Utrecht",
    "10.3389":  "Frontiers",
    "10.3390":  "MDPI",
    "10.48550": "arXiv",
    "10.5194":  "Copernicus",
    "10.6084":  "Figshare",
}

# 已知全部 OA 的出版商（每篇文章都是 OA）
ALWAYS_OA_PUBLISHERS = {"MDPI", "Frontiers", "PLOS", "Copernicus", "Figshare", "arXiv", "bioRxiv"}

# 已知强 WAF 出版商（httpx 被拦，仅 Sci-Hub 或浏览器）
WAF_PUBLISHERS = {"Elsevier", "ACS", "Nature", "Science", "IEEE", "RSC"}


def _check_article_oa(paper) -> tuple[bool, str]:
    """检查单篇文章的 OA 状态.

    优先级:
      1. paper.is_oa (由 OpenAlex 搜索时设置)
      2. 已知全部 OA 的出版商
      3. 兜底: 假阳性比假阴性好, 默认 False

    Returns:
        (is_oa, oa_url)
    """
    # 1. OpenAlex 已标记的 OA 状态
    if hasattr(paper, 'is_oa') and paper.is_oa:
        oa_url = getattr(paper, 'oa_url', '') or ''
        return (True, oa_url)

    # 2. 已知全部 OA 的出版商
    doi = getattr(paper, 'doi', '') or ''
    publisher, _ = _classify_publisher(doi)
    if publisher in ALWAYS_OA_PUBLISHERS:
        return (True, '')

    # 3. 默认非 OA
    return (False, '')


def _classify_publisher(doi: str) -> tuple[str, str]:
    """根据 DOI 判断出版商.

    Returns:
        (publisher_name, doi_prefix)
        如 ("Elsevier", "10.1016"), 未知返回 ("Unknown", "")
    """
    if not doi:
        return ("Unknown", "")
    doi = doi.strip().replace("https://doi.org/", "").replace("http://doi.org/", "")
    # DOI 注册前缀是斜杠前的完整 token；不要用字符串截断做模糊匹配。
    prefix = doi.split("/", 1)[0].lower()
    publisher = PUBLISHER_BY_DOI.get(prefix)
    if publisher:
        return (publisher, prefix)
    return ("Unknown", "")


def _run_strategy(strategy_name: str, doi: str, paper, output_dir: str) -> tuple:
    """执行单个策略，返回 (pdf_path, text, source).

    Args:
        strategy_name: 策略名
        doi: 干净的 DOI
        paper: Paper 对象
        output_dir: 输出目录

    Returns:
        (pdf_path, text, source) 或 (None, None, None) 失败
    """
    strategies = {
        "arxiv": lambda: _try_arxiv(paper, output_dir),
        "unpaywall": lambda: _try_unpaywall(doi, output_dir),
        "scihub": lambda: (_ := fetch_pdf_from_scihub(doi, output_dir=output_dir),
                           (_ and _extract_text(_), "scihub") if _ else (None, None)),
        "publisher_direct": lambda: (_ := fetch_pdf_from_publisher_direct(doi, output_dir=output_dir),
                                     (_ and _extract_text(_), "publisher_direct") if _ else (None, None)),
        "browser": lambda: (_ := fetch_pdf_via_browser(doi, output_dir=output_dir),
                            (_ and _extract_text(_), "browser") if _ else (None, None)),
        "pdf_url": lambda: _try_pdf_url(paper, doi, output_dir),
    }

    if strategy_name not in strategies:
        return (None, None, None)

    try:
        result = strategies[strategy_name]()
        if result and result[0]:
            return (result[0], result[1], strategy_name)
    except Exception:
        logger.warning("full-text strategy %s failed for %s", strategy_name, doi, exc_info=True)
    return (None, None, None)


def _try_arxiv(paper, output_dir: str) -> tuple:
    """尝试从 arXiv 下载 PDF."""
    arxiv_id = ""
    # 从 source 判断
    if hasattr(paper, 'source') and (paper.source or "").lower() == "arxiv":
        url = getattr(paper, 'url', "") or ""
        m = re.search(r'arxiv\.org/abs/(\d{4}\.\d{4,5})', url)
        if m:
            arxiv_id = m.group(1)
    if not arxiv_id:
        doi = (getattr(paper, 'doi', "") or "")
        m = re.search(r'10\.48550/[Aa][Rr][Xx][Ii][Vv]\.(\d{4}\.\d{4,5})', doi)
        if m:
            arxiv_id = m.group(1)
    if not arxiv_id:
        pdf_url = getattr(paper, 'pdf_url', "") or ""
        m = re.search(r'arxiv\.org/pdf/(\d{4}\.\d{4,5})', pdf_url)
        if m:
            arxiv_id = m.group(1)
    if not arxiv_id:
        url = getattr(paper, 'url', "") or ""
        m = re.search(r'arxiv\.org/abs/(\d{4}\.\d{4,5})', url)
        if m:
            arxiv_id = m.group(1)
    if arxiv_id:
        pdf_path = fetch_arxiv_pdf(arxiv_id, output_dir)
        if pdf_path:
            text = _extract_text(pdf_path)
            print(f"  [arXiv] ✅ {arxiv_id[:20]}... ({len(text)} 字符)")
            return (pdf_path, text)
    return (None, None)


def _try_unpaywall(doi: str, output_dir: str) -> tuple:
    """尝试 Unpaywall OA."""
    upw = fetch_full_text_via_unpaywall(doi, output_dir)
    if upw.get("text") and len(upw["text"]) > 500:
        return (upw["pdf_path"], upw["text"])
    return (None, None)


def _try_pdf_url(paper, doi: str, output_dir: str) -> tuple:
    """尝试从 paper.pdf_url 直接下载."""
    pdf_url = getattr(paper, 'pdf_url', "") or ""
    if not pdf_url or pdf_url.startswith("https://doi.org/"):
        return (None, None)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/pdf,*/*",
        }
        client_kwargs = {"timeout": 30, "follow_redirects": True, "http1": True, "http2": False}
        if HTTP_PROXY:
            client_kwargs["proxy"] = HTTP_PROXY
        safe_name = _cache_filename(doi or pdf_url[-30:]).replace('.pdf', '_via_url.pdf')
        output_path = os.path.join(output_dir, safe_name)
        with httpx.Client(**client_kwargs) as client:
            pdf_resp = client.get(pdf_url, headers=headers)
        if pdf_resp.status_code == 200 and len(pdf_resp.content) > 5000 and pdf_resp.content[:5] == b"%PDF-":
            with open(output_path, "wb") as f:
                f.write(pdf_resp.content)
            text = _extract_text(output_path)
            if len(text) > 500:
                try:
                    import fitz
                    doc = fitz.open(output_path)
                    pages = len(doc)
                    doc.close()
                    print(f"  [PDF_URL] ✅ {pdf_url[:50]}... ({pages} 页, {len(text)} 字符)")
                    return (output_path, text)
                except ImportError:
                    logger.info("fitz unavailable for PDF URL extraction")
    except Exception as exc:
        logger.warning("PDF URL strategy failed for %s: %s", doi, exc)
    return (None, None)


def fulltext_capabilities() -> dict:
    """返回本地全文渠道能力，不发起网络请求。"""
    try:
        import fitz  # noqa: F401
        fitz_available = True
    except ImportError:
        fitz_available = False
    try:
        import playwright  # noqa: F401
        browser_available = True
    except ImportError:
        browser_available = False
    return {
        "arxiv": True,
        "unpaywall": bool(UNPAYWALL_EMAIL),
        "scihub": os.environ.get("HARNESS_ENABLE_SCIHUB", "1") == "1",
        "browser": browser_available,
        "pdf_extraction": fitz_available,
        "proxy_configured": bool(HTTP_PROXY),
    }


def get_full_text(paper, output_dir: str | None = None) -> dict:
    """获取论文全文的统一接口（出版商感知策略）.

    根据出版商选择最优策略链，避免对强 WAF 出版商做无谓的尝试：
      - 所有出版商：先走 arXiv/Unpaywall/出版商/浏览器等合法来源
      - 默认按 arXiv → Sci-Hub → Unpaywall 逐个尝试；旧出版社策略可显式开启

    Returns:
        {"pdf_path": "", "text": "", "source": "...", "model_figures": [...]}
    """
    result = {"pdf_path": "", "text": "", "source": "none", "model_figures": [],
              "attempted_strategies": [], "failure_reasons": {}}

    doi = getattr(paper, 'doi', "") or ""
    output_dir = paper_cache_dir(output_dir)
    publisher, _ = _classify_publisher(doi)
    article_is_oa, article_oa_url = _check_article_oa(paper)
    # arXiv 本身是开放存档；其 DOI 形式通常为 10.48550/arXiv.*。
    paper_url = str(getattr(paper, "url", "") or "").lower()
    arxiv_known_oa = "arxiv.org" in paper_url or doi.lower().startswith("10.48550/arxiv.")

    # 0. 手动缓存检查（所有论文都先检查）
    if doi:
        # 新命名
        safe_name = _cache_filename(doi)
        cache_path = os.path.join(output_dir, safe_name)
        # 兼容旧命名（无出版商前缀），但同样必须通过 PDF 魔数校验。
        old_name = re.sub(r'[^\w\-.]', '_', doi.strip().
                          replace("https://doi.org/", "").replace("http://doi.org/", "")) + ".pdf"
        if not _valid_pdf_cache(cache_path):
            old_path = os.path.join(output_dir, old_name)
            if _valid_pdf_cache(old_path):
                cache_path = old_path
        # 兼容 v0.9 之前的 ~/survey-harness/papers 落点（只读回退，不再往那写）
        if not _valid_pdf_cache(cache_path):
            for candidate in (safe_name, old_name):
                legacy_hit = legacy_paper_path(candidate)
                if legacy_hit:
                    cache_path = legacy_hit
                    break
        if _valid_pdf_cache(cache_path):
            text = _extract_text(cache_path)
            if len(text) > 500:
                try:
                    import fitz
                    doc = fitz.open(cache_path)
                    pages = len(doc)
                    doc.close()
                    if pages >= 2:
                        print(f"  [Cache] ✅ 缓存: {doi[:40]}... ({pages} 页, {len(text)} 字符)")
                        try:
                            from extract_model_figures import extract_model_figures
                            model_figs = extract_model_figures(cache_path, max_pages=6)
                            return {"pdf_path": cache_path, "text": text,
                                    "source": "cache", "model_figures": model_figs}
                        except Exception as exc:
                            logger.warning("model figure extraction failed for %s: %s", doi, exc)
                        return {"pdf_path": cache_path, "text": text, "source": "cache"}
                except ImportError:
                    pass

    # 未被来源明确标记为 OA 时，先用 Unpaywall 做元数据判定。
    # 非 OA 论文不进入 Sci-Hub/浏览器等全文下载策略。
    oa_probe = None
    if doi and not article_is_oa and not arxiv_known_oa:
        oa_probe = fetch_full_text_via_unpaywall(doi, output_dir)
        if oa_probe.get("text") and len(oa_probe.get("text", "")) > 500:
            return {"pdf_path": oa_probe["pdf_path"], "text": oa_probe["text"],
                    "source": oa_probe.get("source", "unpaywall"),
                    "is_oa": True, "oa_status": oa_probe.get("oa_status", ""),
                    "oa_url": oa_probe.get("oa_url", "")}
        if oa_probe.get("oa_check") == "oa" or oa_probe.get("is_oa"):
            article_is_oa = True
            article_oa_url = oa_probe.get("oa_url", "") or oa_probe.get("oa_landing_url", "")
        elif oa_probe.get("oa_check") == "not_oa":
            return {"pdf_path": "", "text": "", "source": "not_oa",
                    "is_oa": False, "oa_status": oa_probe.get("oa_status", "")}
        else:
            return {"pdf_path": "", "text": "", "source": "oa_unknown",
                    "is_oa": False, "oa_status": oa_probe.get("oa_status", ""),
                    "oa_reason": "unpaywall_unavailable_or_unconfigured"}

    result.update({
        "is_oa": bool(article_is_oa or arxiv_known_oa),
        "oa_status": (oa_probe or {}).get("oa_status", "") if oa_probe else "",
        "oa_url": article_oa_url or "",
    })

    # 根据 OA 状态和出版商选择策略链。默认只启用 arXiv + Sci-Hub + Unpaywall；
    # 出版商、MDPI 等旧策略保留在代码中，设置
    # HARNESS_ENABLE_PUBLISHER_FETCH=1 后才重新启用。
    _publisher_fetch_enabled = os.environ.get("HARNESS_ENABLE_PUBLISHER_FETCH", "0") == "1"
    if not _publisher_fetch_enabled:
        strategy_chain = ["arxiv", "scihub", "unpaywall", "browser"]
        print("  [Fulltext] 使用精简策略: arXiv → Sci-Hub → Unpaywall → 浏览器落地页")
    elif publisher in WAF_PUBLISHERS or publisher in ("Springer", "Elsevier", "Wiley", "TaylorFrancis"):
        # WAF/Elsevier/Springer: 直接 Edge CDP HTML 提取（不尝试 PDF 下载）
        strategy_chain = ["arxiv", "unpaywall", "edge_cdp", "publisher_direct", "browser"]
        print(f"  [{publisher}] 📄 Edge CDP HTML 提取...")
    elif publisher == "MDPI":
        strategy_chain = ["arxiv", "unpaywall", "edge_cdp", "browser", "publisher_direct"]
        print(f"  [MDPI] 📄 Edge CDP HTML 提取...")
    elif article_is_oa and article_oa_url and not article_oa_url.startswith("https://doi.org/"):
        # 有真实 OA URL 且非 WAF 出版商 → 直接下载 PDF
        strategy_chain = ["arxiv", "pdf_url", "publisher_direct", "unpaywall", "browser"]
    else:
        strategy_chain = ["arxiv", "publisher_direct", "unpaywall", "browser"]

    # Sci-Hub 默认启用；设为 0 可临时关闭。仅在有 DOI 时实际尝试。
    if os.environ.get("HARNESS_ENABLE_SCIHUB", "1") != "1":
        strategy_chain = [s for s in strategy_chain if s != "scihub"]

    for strategy in strategy_chain:
        result["attempted_strategies"].append(strategy)
        if strategy == "arxiv":
            pdf_path, text = _try_arxiv(paper, output_dir) or (None, None)
            if pdf_path:
                return {"pdf_path": pdf_path, "text": text, "source": "arxiv"}
        elif strategy == "unpaywall" and doi:
            upw = fetch_full_text_via_unpaywall(doi, output_dir)
            if upw.get("text") and len(upw["text"]) > 500:
                return {"pdf_path": upw["pdf_path"], "text": upw["text"],
                        "source": upw["source"], "oa_status": upw.get("oa_status", "")}
            # Unpaywall 查到 OA 但 httpx 下载失败 → 记住 OA URL 给后续策略
            upw_oa_url = upw.get("oa_url", "")
            if upw.get("is_oa") and (upw_oa_url or upw.get("oa_landing_url")):
                fallback = upw.get("oa_landing_url") or upw_oa_url
                print(f"  [Unpaywall] 🔗 OA 已知，浏览器尝试落地页: {fallback[:60]}")
                result["_upw_oa_url"] = upw_oa_url
                result["_upw_landing_url"] = upw.get("oa_landing_url", "")
                result["_upw_pdf_urls"] = upw.get("oa_pdf_urls", [])
                result["_upw_landing_urls"] = upw.get("oa_landing_urls", [])
        elif strategy == "scihub" and doi:
            pdf_path = fetch_pdf_from_scihub(doi, output_dir=output_dir)
            if pdf_path:
                result["pdf_path"] = pdf_path
                result["text"] = _extract_text(pdf_path)
                result["source"] = "scihub"
                return result
        elif strategy == "publisher_direct" and doi:
            pdf_path = fetch_pdf_from_publisher_direct(doi, output_dir=output_dir)
            if pdf_path:
                result["pdf_path"] = pdf_path
                result["text"] = _extract_text(pdf_path)
                result["source"] = "publisher_direct"
                return result
        elif strategy == "edge_cdp" and doi:
            pdf_path = _fetch_pdf_via_edge_cdp(doi, output_dir)
            if pdf_path:
                result["pdf_path"] = pdf_path
                result["text"] = _extract_text(pdf_path)
                result["source"] = "edge_cdp"
                return result
        elif strategy == "edge_cdp_landing" and doi:
            # 走 DOI 文章页面提取 citation_pdf_url，而非 PDF 直链（避免 Chromium PDF viewer）
            pdf_path = _fetch_pdf_via_edge_cdp_landing(doi, output_dir)
            if pdf_path:
                result["pdf_path"] = pdf_path
                result["text"] = _extract_text(pdf_path)
                result["source"] = "edge_cdp"
                return result
        elif strategy == "browser" and doi:
            # 先试所有 Unpaywall 直链，再试所有落地页；不能因 best location
            # 失败就放弃其他合法 OA 来源。
            browser_urls = []
            browser_urls.extend(result.get("_upw_pdf_urls", []))
            browser_urls.extend(result.get("_upw_landing_urls", []))
            browser_urls.extend([result.get("_upw_oa_url", ""),
                                 result.get("_upw_landing_url", "")])
            browser_urls = list(dict.fromkeys(u for u in browser_urls if u))
            if not browser_urls:
                browser_urls = [""]
            pdf_path = ""
            for browser_url in browser_urls:
                pdf_path = fetch_pdf_via_browser(doi, output_dir=output_dir,
                                                  oa_url=browser_url)
                if pdf_path:
                    break
            if pdf_path:
                result["pdf_path"] = pdf_path
                result["text"] = _extract_text(pdf_path)
                result["source"] = "browser"
                return result
        elif strategy == "pdf_url":
            pdf_path, text = _try_pdf_url(paper, doi, output_dir) or (None, None)
            if pdf_path:
                return {"pdf_path": pdf_path, "text": text, "source": "pdf_url"}

    # 全部失败：索引兜底
    if doi:
        safe_name = _cache_filename(doi, '.json')
        index_path = os.path.join(output_dir, safe_name)
        # 兼容旧 JSON 命名
        if not os.path.exists(index_path):
            old_json = re.sub(r'[^\w\-\.]', '_', doi) + ".json"
            old_jpath = os.path.join(output_dir, old_json)
            if os.path.exists(old_jpath):
                index_path = old_jpath
        
        # 构建索引（摘要已从搜索阶段获得，不用再抓）
        title = getattr(paper, 'title', "") or ""
        authors = getattr(paper, 'authors', []) or []
        abstract = getattr(paper, 'abstract', "") or ""
        venue = getattr(paper, 'venue', "") or ""
        keywords = getattr(paper, 'keywords', []) or []
        citations = getattr(paper, 'citations', 0) or 0
        source = getattr(paper, 'source', "") or ""
        
        index_data = {
            "doi": doi,
            "title": title,
            "authors": authors if isinstance(authors, list) else [authors],
            "year": getattr(paper, 'year', 0) or 0,
            "abstract": abstract[:2000] if abstract else "",
            "venue": venue,
            "keywords": keywords,
            "citations": citations,
            "source": source,
            "status": "仅索引",
            "note": "未获取到全文 PDF，仅保存元数据"
        }
        
        with open(index_path, "w", encoding="utf-8") as f:
            import json
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        
        result["pdf_path"] = index_path
        result["source"] = "index"
        result["text"] = abstract[:2000] if abstract else title
        print(f"  [Index] 📝 仅索引: {doi[:40]}... (无全文)")
    
    # 后处理：对成功下载的 PDF 自动提取模型图
    if result.get("pdf_path") and result["pdf_path"].endswith(".pdf"):
        try:
            from extract_model_figures import extract_model_figures
            model_figs = extract_model_figures(result["pdf_path"], max_pages=6)
            if model_figs:
                result["model_figures"] = model_figs
                print(f"  [ModelFig] 🖼️ 提取 {len(model_figs)} 张模型图")
        except Exception as exc:
            logger.warning("model figure extraction failed for %s: %s", doi, exc)
    
    return result