"""Polite fetcher: robots.txt, per-domain rate limiting, retries, TLS fallback, HTML+PDF parsing,
an on-disk raw cache (so a run can be replayed offline) and a *labelled* overlay hook used only by the demo."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.robotparser
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.exceptions import SSLError

from . import config
from .textproc import Doc, Link, html_to_doc, pdf_to_doc
from .util import canonical_url, host_of, registered_domain, sha1

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
try:  # urllib3 v2 emits its own category
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # pragma: no cover
    pass

MAX_BYTES = 30 * 1024 * 1024


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0                    # HTTP status, 0 = network failure, -1 = robots/deny
    content_type: str = ""
    text: str = ""
    title: str = ""
    headings: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    content_hash: str = ""
    error: str = ""
    tls_verified: bool = True
    last_modified: str = ""
    needs_ocr: bool = False
    fetched_at: str = ""
    from_cache: bool = False
    simulated: bool = False
    is_pdf: bool = False
    rendered: bool = False            # text obtained by executing JavaScript (Playwright)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)

    @property
    def gone(self) -> bool:
        """Hard evidence the page was removed (as opposed to a transient failure)."""
        return self.status in (404, 410)

    @property
    def transient(self) -> bool:
        return self.status in (-1, 0, 401, 403, 406, 408, 425, 429, 451) or self.status >= 500


@dataclass
class Overlay:
    """A deliberately artificial modification of a source page (DEMO ONLY, always flagged simulated)."""
    status: int | None = None
    replacements: list[tuple[str, str]] = field(default_factory=list)   # (regex, replacement)
    note: str = ""


class Fetcher:
    def __init__(self, mode: str = "live", cache_dir: Path | None = None, overlays: dict[str, Overlay] | None = None):
        cfg = config.settings()["crawl"]
        self.cfg = cfg
        self.mode = mode                      # live | cache (offline replay)
        self.cache_dir = Path(cache_dir or config.CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.overlays = {canonical_url(k): v for k, v in (overlays or {}).items()}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-IN,en;q=0.8",
        })
        self._last_hit: dict[str, float] = {}
        self._pw = None
        self._browser = None
        self._js_count = 0
        self._dead: dict[str, int] = {}          # registered domain -> consecutive connect failures (circuit breaker)
        self.connect_timeout = float(cfg.get("connect_timeout_seconds", 6))
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.stats = {"fetched": 0, "cache_hits": 0, "robots_blocked": 0, "errors": 0}

    # ------------------------------------------------------------------ robots / pacing
    def _allowed(self, url: str) -> bool:
        if not self.cfg.get("respect_robots", True) or self.mode == "cache":
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=(self.connect_timeout, 8), verify=False)
                if r.status_code == 200 and "html" not in r.headers.get("content-type", "") and len(r.text) < 200_000:
                    rp.parse(r.text.splitlines())
                    self._robots[base] = rp
                else:
                    self._robots[base] = None
            except requests.RequestException:
                self._robots[base] = None
        rp = self._robots[base]
        if rp is None:
            return True
        return rp.can_fetch(self.cfg["user_agent"], url) or rp.can_fetch("*", url)

    def _pace(self, url: str) -> None:
        dom = registered_domain(host_of(url))
        delay = float(self.cfg["per_domain_delay_seconds"])
        with self._lock:
            wait = self._last_hit.get(dom, 0) + delay - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[dom] = time.time()

    # ------------------------------------------------------------------ cache
    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        k = sha1(canonical_url(url))
        return self.cache_dir / f"{k}.bin", self.cache_dir / f"{k}.json"

    def _cache_write(self, url: str, content: bytes, meta: dict) -> None:
        b, m = self._cache_paths(url)
        try:
            b.write_bytes(content)
            m.write_text(json.dumps(meta), encoding="utf-8")
        except OSError:
            pass

    def _cache_read(self, url: str) -> tuple[bytes, dict] | None:
        b, m = self._cache_paths(url)
        if b.exists() and m.exists():
            try:
                return b.read_bytes(), json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return None

    # ------------------------------------------------------------------ main entry
    def fetch(self, url: str) -> FetchResult:
        res = self._fetch(url)
        ov = self.overlays.get(canonical_url(url)) or self.overlays.get(canonical_url(res.final_url or url))
        if ov:
            res = self._apply_overlay(res, ov)
        return res

    def _fetch(self, url: str) -> FetchResult:
        res = FetchResult(url=url, fetched_at=config.now_iso())
        if self.mode == "cache":
            cached = self._cache_read(url)
            if not cached:
                res.status, res.error = 0, "not in offline cache"
                return res
            content, meta = cached
            self.stats["cache_hits"] += 1
            res.from_cache = True
            return self._build(res, content, meta)

        if not self._allowed(url):
            self.stats["robots_blocked"] += 1
            res.status, res.error = -1, "blocked by robots.txt"
            return res

        last_err = ""
        dom = registered_domain(host_of(url))
        if self._dead.get(dom, 0) >= 2:
            self.stats["errors"] += 1
            res.status, res.error = 0, "domain unreachable this run (circuit breaker open)"
            return res
        tmo = (self.connect_timeout, self.cfg["timeout_seconds"])
        for attempt in range(3):
            self._pace(url)
            verify = True
            try:
                try:
                    r = self.session.get(url, timeout=tmo, stream=True, allow_redirects=True, verify=True)
                except SSLError:
                    verify = False
                    r = self.session.get(url, timeout=tmo, stream=True, allow_redirects=True, verify=False)
                self._dead[dom] = 0
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                    chunks.append(chunk)
                content = b"".join(chunks)
                meta = {
                    "status": r.status_code, "final_url": r.url,
                    "content_type": r.headers.get("content-type", ""),
                    "last_modified": r.headers.get("last-modified", ""),
                    "tls_verified": verify, "fetched_at": res.fetched_at,
                }
                if r.status_code >= 500 and attempt < 2:
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                self.stats["fetched"] += 1
                if r.status_code < 400:
                    self._cache_write(url, content, meta)
                    if canonical_url(r.url) != canonical_url(url):
                        self._cache_write(r.url, content, meta)
                built = self._build(res, content, meta)
                return self._maybe_render(built, url, meta)
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:160]}"
                if isinstance(exc, (requests.ConnectTimeout, requests.ConnectionError)):
                    self._dead[dom] = self._dead.get(dom, 0) + 1
                    break                          # a host that will not accept connections is not retried
                if isinstance(exc, requests.Timeout):          # accepted the connection but never answered
                    self._dead[dom] = self._dead.get(dom, 0) + 1
                    if attempt >= 1 or self._dead[dom] >= 2:
                        break
                time.sleep(1.2 * (attempt + 1))
        self.stats["errors"] += 1
        res.status, res.error = 0, last_err
        return res

    # ------------------------------------------------------------------ optional JavaScript rendering (Playwright, free)
    def _maybe_render(self, res: FetchResult, url: str, meta: dict) -> FetchResult:
        cfg = self.cfg
        if (not cfg.get("js_render", True) or self.mode != "live" or res.is_pdf or not (200 <= res.status < 300)
                or "html" not in res.content_type or len(res.text) >= int(cfg.get("js_render_min_chars", 400))
                or self._js_count >= int(cfg.get("max_js_renders", 40))):
            return res
        html = self._render_js(res.final_url or url)
        if not html:
            return res
        before = (res.text, res.title, res.headings, res.links, res.content_hash)
        raw = html.encode("utf-8", "ignore")
        self._build(res, raw, {**meta, "content_type": "text/html; charset=utf-8"})
        if len(res.text) <= len(before[0]) * 1.5:
            res.text, res.title, res.headings, res.links, res.content_hash = before      # rendering did not help
            return res
        res.rendered = True
        self.stats["js_rendered"] = self.stats.get("js_rendered", 0) + 1
        self._cache_write(url, raw, {**meta, "content_type": "text/html; charset=utf-8", "rendered": True})
        if canonical_url(res.final_url or url) != canonical_url(url):
            self._cache_write(res.final_url, raw, {**meta, "content_type": "text/html; charset=utf-8", "rendered": True})
        return res

    def _render_js(self, url: str) -> str | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.cfg["js_render"] = False
            return None
        if self._pw is None:
            try:
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch()
            except Exception:                       # browser binary missing: degrade gracefully
                self._pw = None
                self.cfg["js_render"] = False
                return None
        self._js_count += 1
        ctx = None
        try:
            ctx = self._browser.new_context(user_agent=self.cfg["user_agent"], ignore_https_errors=True)
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(1200)
            return page.content()
        except Exception:
            return None
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = None

    def _build(self, res: FetchResult, content: bytes, meta: dict) -> FetchResult:
        res.status = int(meta.get("status", 0))
        res.final_url = meta.get("final_url", res.url)
        res.content_type = (meta.get("content_type") or "").lower()
        res.last_modified = meta.get("last_modified", "")
        res.tls_verified = bool(meta.get("tls_verified", True))
        if meta.get("fetched_at") and res.from_cache:
            res.fetched_at = meta["fetched_at"]
        res.content_hash = sha1(content)[:20]
        if res.status >= 400 or not content:
            res.error = res.error or f"HTTP {res.status}"
            return res
        is_pdf = "pdf" in res.content_type or res.final_url.lower().split("?")[0].endswith(".pdf") or content[:5] == b"%PDF-"
        res.is_pdf = is_pdf
        try:
            if is_pdf:
                doc = pdf_to_doc(content, int(self.cfg["max_pdf_pages"]))
            elif "html" in res.content_type or "xml" in res.content_type or not res.content_type or content.lstrip()[:1] == b"<":
                doc = html_to_doc(_decode(content, res.content_type), res.final_url or res.url)
            else:
                doc = Doc()
                res.error = f"unsupported content-type {res.content_type}"
        except Exception as exc:  # parser blow-ups must never kill a run
            doc = Doc()
            res.error = f"parse error: {type(exc).__name__}"
        body = doc.text[: int(self.cfg["max_text_chars"])]
        # The <title>/PDF title is part of the page; make it part of the evidence text so offsets stay consistent.
        if doc.title and body and doc.title not in body[:600]:
            body = doc.title + "\n" + body
        res.text = body
        res.title, res.headings, res.links, res.needs_ocr = doc.title, doc.headings, doc.links, doc.needs_ocr
        if res.text:   # hash the cleaned text (stable across cookie/CSRF/timestamp noise in raw HTML)
            res.content_hash = sha1(re.sub(r"\s+", " ", res.text))[:20]
        return res

    def probe(self, url: str) -> dict:
        """Reachability of an application URL (status only, body not downloaded). None status = not checked."""
        key = canonical_url(url.split("#")[0])
        cache = self.__dict__.setdefault("_probe_cache", {})
        if key in cache:
            return cache[key]
        out: dict = {"status": None, "error": ""}
        ov = self.overlays.get(key)
        if ov and ov.status is not None:
            out = {"status": ov.status, "error": "simulated"}
        elif self.mode == "cache":
            cached = self._cache_read(url)
            out = {"status": int(cached[1].get("status", 0)) if cached else None, "error": "" if cached else "offline"}
        else:
            self._pace(url)
            for verify in (True, False):
                try:
                    r = self.session.get(url.split("#")[0], timeout=(self.connect_timeout, 15), stream=True, allow_redirects=True, verify=verify)
                    out = {"status": r.status_code, "error": "", "final_url": r.url}
                    r.close()
                    break
                except SSLError:
                    continue
                except requests.RequestException as exc:
                    out = {"status": 0, "error": type(exc).__name__}
                    break
        cache[key] = out
        return out

    def _apply_overlay(self, res: FetchResult, ov: Overlay) -> FetchResult:
        res.simulated = True
        if ov.status is not None:
            res.status = ov.status
            res.error = f"SIMULATED HTTP {ov.status}: {ov.note}"
            res.text = ""
            return res
        text = res.text
        for pattern, repl in ov.replacements:
            text = re.sub(pattern, repl, text, count=1)
        res.text = text
        res.content_hash = sha1(text)[:20]
        return res


def _decode(content: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "")
    for enc in ([m.group(1)] if m else []) + ["utf-8", "cp1252"]:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", "ignore")
