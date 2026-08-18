import os
#!/usr/bin/env python3
"""
NOVA Resource Collector v1.0
=============================
NOVA 각 기능 영역의 근본 리소스를 자율 수집·인덱싱·지식 추출하는 엔진.

기능:
  - 도메인별 쿼리 자동 실행 (웹 검색, arXiv, GitHub, HackerNews, RSS)
  - 수집 결과 raw/ 저장 + index.json 인덱싱
  - 중복 제거 + 중요도 스코어링
  - knowledge.md 누적 지식 추출 (Claude API)
  - 크로스 프로젝트 인사이트 공유

사용법:
  python3 nova_resource_collector.py collect <project>
  python3 nova_resource_collector.py learn   <project>
  python3 nova_resource_collector.py index   <project>
  python3 nova_resource_collector.py cross                  # 크로스 인사이트 생성
  python3 nova_resource_collector.py status  [project]      # 리소스 현황
"""

import sys, os, json, time, hashlib, logging, re, subprocess, ssl
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── 기본 경로 ──────────────────────────────────────────────────────────────
HERMES_DIR   = Path(os.environ.get("HERMES_DIR", os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))))
PROJECTS_DIR = Path(os.environ.get("NOVA_PROJECTS_DIR", str(HERMES_DIR / "projects")))
SCRIPTS_DIR  = HERMES_DIR / "scripts"
KB_DIR       = HERMES_DIR / "kb"
LOGS_DIR     = HERMES_DIR / "logs" / "nova"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Claude API — 사설 게이트웨이 기본값 없음, 환경변수 필수
CLAUDE_API_URL   = os.environ.get("CLAUDE_API_URL", "")
CLAUDE_API_MODEL = os.environ.get("CLAUDE_API_MODEL", "claude-sonnet-5")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
# P1 fix (2026-08-18, Codex-audited): this used to default SSL_VERIFY to
# False (verification OFF) unless NOVA_FORCE_SSL_VERIFY was set to any
# non-empty string — appropriate only for the original author's internal
# network with a self-signed gateway cert, and inconsistent with the
# NOVA_DISABLE_SSL_VERIFY=1 explicit opt-out pattern used everywhere else
# in this codebase (nova/providers/llm.py, nova/agents/bin/nova_llm.py,
# nova/agents/bin/nova_brain.py, etc). Default to verification ON; opt out
# explicitly via NOVA_DISABLE_SSL_VERIFY=1 for a self-signed endpoint.
_ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE", "")
SSL_VERIFY = _ca_bundle if _ca_bundle else (
    os.environ.get("NOVA_DISABLE_SSL_VERIFY", "").strip().lower()
    not in ("1", "true", "yes", "on")
)

# RC-1 fix: urllib.urlopen SSL 컨텍스트 헬퍼 (urllib은 verify= 파라미터 없음)
def _ssl_ctx() -> ssl.SSLContext | None:
    """SSL_VERIFY 설정에 따라 SSLContext 반환.
    SSL_VERIFY=False → unverified context (사내 self-signed cert 허용).
    SSL_VERIFY=str(CA bundle path) → cafile 지정 context.
    SSL_VERIFY=True → None (기본 Python SSL 검증).
    """
    if SSL_VERIFY is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if isinstance(SSL_VERIFY, str) and SSL_VERIFY:
        ctx = ssl.create_default_context(cafile=SSL_VERIFY)
        return ctx
    return None  # 기본 검증

# [R10-CC-007-FIX] 네트워크 timeout 환경변수화 (하드코딩 제거)
NOVA_COLLECTOR_TIMEOUT = int(os.environ.get("NOVA_COLLECTOR_TIMEOUT", "10"))
NOVA_API_TIMEOUT = int(os.environ.get("NOVA_API_TIMEOUT", "60"))

KST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "resource_collector.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("nova.resources")

# ── 도메인별 리소스 쿼리 정의 ─────────────────────────────────────────────
# 각 프로젝트 + 기능 영역별 검색 쿼리 목록
DOMAIN_QUERIES = {
    # ── 블로그 파이프라인 ──
    "blog-pipeline": {
        "description": "여행 블로그 SEO 자동화 파이프라인",
        "domains": {
            "seo_strategy": [
                "travel blog SEO strategy 2025",
                "Korea travel blog keyword optimization",
                "long-tail travel keywords affiliate",
                "여행 블로그 SEO 최신 트렌드",
                "구글 SEO 알고리즘 업데이트 2025",
            ],
            "content_quality": [
                "travel content E-E-A-T guidelines Google",
                "AI travel content quality scoring methods",
                "travel blog content structure best practices",
                "여행 콘텐츠 품질 기준 구글 가이드",
            ],
            "affiliate_optimization": [
                "travel affiliate link conversion optimization 2025",
                "trip.com affiliate best practices",
                "여행사 제휴마케팅 전환율 최적화",
                "Klook Booking.com affiliate strategy",
            ],
            "technical_seo": [
                "naver blog SEO algorithm 2025",
                "tistory SEO optimization tips",
                "tistory 검색 최적화 2025",
                "네이버 블로그 상위노출 전략",
            ],
            "automation": [
                "content automation pipeline LLM quality control",
                "AI blog writing workflow best practices",
                "automated content publishing SEO safety",
            ],
        },
        "arxiv_queries": [
            "automated content generation quality evaluation",
            "SEO text quality LLM",
            "travel recommendation natural language generation",
        ],
        "github_queries": [
            "blog SEO automation",
            "travel content generator",
            "affiliate link optimizer",
        ],
        "rss_feeds": [
            "https://moz.com/blog/feed",
            "https://ahrefs.com/blog/rss/",
            "https://searchengineland.com/feed",
        ],
    },

    # ── doosi 채널 ──
    "doosi": {
        "description": "나도너도 김두시 숏폼 콘텐츠 자동화",
        "domains": {
            "shorts_strategy": [
                "YouTube Shorts algorithm 2025 strategy",
                "Instagram Reels viral content pattern",
                "한국 유튜브 쇼츠 바이럴 전략 2025",
                "숏폼 콘텐츠 알고리즘 최신 트렌드",
            ],
            "emotional_content": [
                "emotional storytelling short video script",
                "vicarious experience content design",
                "대리만족 콘텐츠 심리학",
                "감성 마케팅 숏폼 전략",
            ],
            "ai_video": [
                "AI video generation Runway Sora best practices 2025",
                "AI generated content YouTube policy 2025",
                "Grok image generation cinematic style",
                "AI 영상 생성 최신 도구 비교",
            ],
            "tts_voiceover": [
                "Korean TTS natural voice generation 2025",
                "ElevenLabs Edge TTS comparison quality",
                "한국어 TTS 자연스러움 평가 기준",
            ],
            "channel_growth": [
                "YouTube channel growth strategy 2025",
                "Instagram growth hack 2025 reels",
                "1만 구독자 달성 전략 유튜브",
            ],
        },
        "arxiv_queries": [
            "video script generation emotional engagement",
            "short video content quality assessment",
            "multimodal content generation evaluation",
        ],
        "github_queries": [
            "youtube shorts automation",
            "video content generator AI",
            "TTS video pipeline",
        ],
        "rss_feeds": [
            "https://socialmediaexaminer.com/feed/",
            "https://www.tubics.com/blog/feed/",
        ],
    },

    # ── 언러닝 ──
    "unlearning": {
        "description": "노이반 언러닝 성장 프로젝트",
        "domains": {
            "learning_science": [
                "unlearning psychology cognitive science 2025",
                "habit change neuroscience latest research",
                "mental model restructuring methods",
                "인지적 언러닝 심리학 최신 연구",
                "습관 변화 뇌과학 2025",
            ],
            "growth_frameworks": [
                "personal growth framework AI coaching",
                "behavior change intervention design",
                "성장 마인드셋 행동변화 프레임워크",
            ],
            "content_strategy": [
                "personal growth content writing strategy",
                "vulnerability storytelling engagement",
                "자기계발 콘텐츠 공감 전략",
            ],
        },
        "arxiv_queries": [
            "machine unlearning cognitive science",
            "behavior change intervention effectiveness",
            "habit formation neural mechanisms",
        ],
        "github_queries": [],
        "rss_feeds": [],
    },

    # ── NOVA 운영 자체 (개발/감사/테스팅 영역) ──
    "_nova_ops": {
        "description": "NOVA 시스템 개발·감사·점검·테스팅 기반 리소스",
        "domains": {
            "llm_code_review": [
                "LLM code review automation best practices 2025",
                "AI code audit static analysis integration",
                "automated code quality gate LLM",
                "Claude Code review pipeline production",
            ],
            "autonomous_agents": [
                "autonomous AI agent architecture 2025",
                "multi-agent orchestration patterns LLM",
                "self-improving AI system design",
                "agentic AI loop best practices",
            ],
            "testing_strategy": [
                "AI pipeline testing strategy 2025",
                "LLM output quality testing automated",
                "content pipeline integration testing",
                "harness-driven testing pattern",
            ],
            "observability": [
                "AI agent observability metrics 2025",
                "LLM pipeline monitoring best practices",
                "autonomous system health check patterns",
            ],
            "evolution_patterns": [
                "self-evolving software system design",
                "adaptive AI pipeline architecture",
                "harness.md driven development pattern",
                "evolutionary software engineering AI",
            ],
        },
        "arxiv_queries": [
            "autonomous software engineering agent",
            "self-improving code generation system",
            "AI agent evaluation benchmark",
            "LLM pipeline quality assurance",
        ],
        "github_queries": [
            "self-improving AI agent",
            "autonomous code review pipeline",
            "LLM evaluation framework",
            "AI harness testing",
        ],
        "rss_feeds": [
            "https://openai.com/blog/rss/",
            "https://www.anthropic.com/rss.xml",
            "https://huggingface.co/blog/feed.xml",
        ],
    },
}


def _load_api_key() -> str:
    """HERMES_API_KEY 로드"""
    key = os.environ.get("HERMES_API_KEY", "")
    if not key:
        env_file = HERMES_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("HERMES_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key


def _resources_dir(project: str) -> Path:
    """프로젝트별 resources 디렉토리"""
    # [R10-CC-006-FIX] _nova_ops 경로를 PROJECTS_DIR로 통일 (HERMES_DIR 하드코딩 제거)
    d = PROJECTS_DIR / project / "resources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _raw_dir(project: str) -> Path:
    d = _resources_dir(project) / "raw"
    d.mkdir(exist_ok=True)
    return d


def _index_path(project: str) -> Path:
    return _resources_dir(project) / "index.json"


def _knowledge_path(project: str) -> Path:
    return _resources_dir(project) / "knowledge.md"


def _cross_insights_path() -> Path:
    # RC-3 fix: PROJECTS_DIR 기반 경로 (HERMES_DIR 하드코딩 제거)
    p = PROJECTS_DIR / "_nova_ops" / "resources"
    p.mkdir(parents=True, exist_ok=True)
    return p / "cross_insights.md"


def load_index(project: str) -> list:
    p = _index_path(project)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_index(project: str, entries: list):
    # [R10-CC-001-FIX] 원자적 쓰기 — tempfile+os.replace
    import tempfile as _tf, os as _os
    index_path = _index_path(project)
    _fd, _tmp = _tf.mkstemp(dir=str(index_path.parent), suffix=".json.tmp")
    try:
        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _fd = -1
            json.dump(entries, _f, ensure_ascii=False, indent=2)
        _os.replace(_tmp, str(index_path))
    except Exception:
        if _fd != -1:
            try: _os.close(_fd)
            except OSError: pass
        try: _os.unlink(_tmp)
        except OSError: pass
        raise


def url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _web_search(query: str, max_results: int = 5) -> list:
    """웹 검색 — DuckDuckGo HTML 파싱 (라이브러리 불필요)"""
    results = []
    try:
        import urllib.request, urllib.parse, html
        ua = "Mozilla/5.0 (compatible; HermesBot/1.0)"
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=NOVA_COLLECTOR_TIMEOUT, context=_ssl_ctx()) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        # 링크 + 제목 추출
        link_pat = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
        snippet_pat = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
        links = link_pat.findall(body)
        snippets = snippet_pat.findall(body)
        for i, (href, title) in enumerate(links[:max_results]):
            clean_title = re.sub(r"<[^>]+>", "", title).strip()
            clean_title = html.unescape(clean_title)
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
                snippet = html.unescape(snippet)
            results.append({
                "url": href,
                "title": clean_title,
                "snippet": snippet[:300],
                "source": "duckduckgo",
            })
    except Exception as e:
        log.debug(f"web_search 실패 ({query}): {e}")
    return results


def _jina_fetch(url: str, timeout: int = 12) -> str:
    """Jina Reader로 URL 내용 가져오기"""
    try:
        import urllib.request
        jina_url = f"https://r.jina.ai/{url}"
        req = urllib.request.Request(
            jina_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.read().decode("utf-8", errors="ignore")[:3000]
    except Exception as e:
        log.debug(f"jina_fetch 실패 ({url}): {e}")
        return ""


def _arxiv_search(query: str, max_results: int = 3) -> list:
    """arXiv Atom API 검색"""
    results = []
    try:
        import urllib.request, urllib.parse
        q = urllib.parse.quote_plus(query)
        url = f"http://export.arxiv.org/api/query?search_query=all:{q}&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"
        with urllib.request.urlopen(url, timeout=NOVA_COLLECTOR_TIMEOUT, context=_ssl_ctx()) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        entries = re.findall(r"<entry>(.*?)</entry>", body, re.S)
        for entry in entries:
            title_m = re.search(r"<title>(.*?)</title>", entry, re.S)
            link_m  = re.search(r'<id>(.*?)</id>', entry)
            summ_m  = re.search(r"<summary>(.*?)</summary>", entry, re.S)
            pub_m   = re.search(r"<published>(.*?)</published>", entry)
            if title_m and link_m:
                results.append({
                    "url": link_m.group(1).strip(),
                    "title": re.sub(r"\s+", " ", title_m.group(1)).strip(),
                    "snippet": re.sub(r"\s+", " ", (summ_m.group(1) if summ_m else ""))[:300].strip(),
                    "source": "arxiv",
                    "published": pub_m.group(1)[:10] if pub_m else "",
                })
    except Exception as e:
        log.debug(f"arxiv_search 실패 ({query}): {e}")
    return results


def _github_search(query: str, max_results: int = 3) -> list:
    """GitHub 공개 검색 API"""
    results = []
    try:
        import urllib.request, urllib.parse
        q = urllib.parse.quote_plus(query)
        url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={max_results}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "HermesBot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=NOVA_COLLECTOR_TIMEOUT, context=_ssl_ctx()) as resp:
            data = json.loads(resp.read().decode())
        for item in data.get("items", [])[:max_results]:
            results.append({
                "url": item.get("html_url", ""),
                "title": item.get("full_name", ""),
                "snippet": (item.get("description") or "")[:200],
                "source": "github",
                "stars": item.get("stargazers_count", 0),
            })
    except Exception as e:
        log.debug(f"github_search 실패 ({query}): {e}")
    return results


def _hn_search(query: str, max_results: int = 3) -> list:
    """HackerNews Algolia 검색"""
    results = []
    try:
        import urllib.request, urllib.parse
        q = urllib.parse.quote_plus(query)
        url = f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage={max_results}"
        with urllib.request.urlopen(url, timeout=NOVA_COLLECTOR_TIMEOUT, context=_ssl_ctx()) as resp:
            data = json.loads(resp.read().decode())
        for hit in data.get("hits", [])[:max_results]:
            results.append({
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                "title": hit.get("title", ""),
                "snippet": f"points={hit.get('points',0)} comments={hit.get('num_comments',0)}",
                "source": "hackernews",
            })
    except Exception as e:
        log.debug(f"hn_search 실패 ({query}): {e}")
    return results


def _rss_fetch(feed_url: str, max_items: int = 3) -> list:
    """RSS/Atom 피드 파싱"""
    results = []
    try:
        import urllib.request
        req = urllib.request.Request(
            feed_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, application/atom+xml"},
        )
        with urllib.request.urlopen(req, timeout=NOVA_COLLECTOR_TIMEOUT, context=_ssl_ctx()) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        # item/entry 파싱
        items = re.findall(r"<item>(.*?)</item>", body, re.S)
        if not items:
            items = re.findall(r"<entry>(.*?)</entry>", body, re.S)
        for item in items[:max_items]:
            title_m = re.search(r"<title[^>]*>(.*?)</title>", item, re.S)
            link_m  = re.search(r"<link[^>]*>(.*?)</link>|<link[^>]+href=['\"]([^'\"]+)['\"]", item, re.S)
            desc_m  = re.search(r"<description[^>]*>(.*?)</description>|<summary[^>]*>(.*?)</summary>", item, re.S)
            if title_m:
                title = re.sub(r"<[^>]+>|<!\[CDATA\[|\]\]>", "", title_m.group(1)).strip()
                href  = ""
                if link_m:
                    href = (link_m.group(1) or link_m.group(2) or "").strip()
                snippet = ""
                if desc_m:
                    raw = desc_m.group(1) or desc_m.group(2) or ""
                    snippet = re.sub(r"<[^>]+>|<!\[CDATA\[|\]\]>", "", raw).strip()[:200]
                results.append({
                    "url": href,
                    "title": title[:200],
                    "snippet": snippet,
                    "source": "rss",
                })
    except Exception as e:
        log.debug(f"rss_fetch 실패 ({feed_url}): {e}")
    return results


def _score_entry(entry: dict) -> float:
    """리소스 중요도 스코어링 (0~100)"""
    score = 50.0
    title = (entry.get("title") or "").lower()
    snippet = (entry.get("snippet") or "").lower()
    text = title + " " + snippet

    # 최신성 보너스
    pub = entry.get("published", "")
    if pub and pub >= "2025":
        score += 15
    elif pub and pub >= "2024":
        score += 8

    # 소스별 기본 신뢰도
    source_bonus = {
        "arxiv": 20, "github": 15, "hackernews": 10,
        "duckduckgo": 5, "rss": 12,
    }
    score += source_bonus.get(entry.get("source", ""), 0)

    # 스타 수 (GitHub)
    stars = entry.get("stars", 0)
    if stars > 1000:
        score += 15
    elif stars > 100:
        score += 8

    # 키워드 관련성 보너스
    high_value_keywords = [
        "2025", "best practice", "production", "automated", "pipeline",
        "evaluation", "quality", "autonomous", "agent", "LLM",
        "seo", "algorithm", "strategy", "research", "study",
    ]
    for kw in high_value_keywords:
        if kw in text:
            score += 3

    return min(score, 100.0)


def cmd_collect(project: str):
    """프로젝트 도메인별 리소스 자동 수집 + 인덱싱"""
    if project not in DOMAIN_QUERIES and project != "_all":
        # 알 수 없는 프로젝트는 _nova_ops로 수집
        log.warning(f"[collect] 알 수 없는 프로젝트: {project} → _nova_ops 도메인 수집")
        project = "_nova_ops"

    targets = list(DOMAIN_QUERIES.keys()) if project == "_all" else [project]
    total_new = 0

    for proj in targets:
        cfg = DOMAIN_QUERIES[proj]
        log.info(f"[collect] {proj} — {cfg['description']} 수집 시작")

        resources_dir = _resources_dir(proj)

        # [R10-CC-002-FIX] 프로젝트별 수집 락
        try:
            import fcntl as _fcntl
            _lock_f = open(resources_dir / ".collect.lock", "a")
            _fcntl.flock(_lock_f, _fcntl.LOCK_EX)
        except (ImportError, OSError):
            _lock_f = None
        try:
            existing = load_index(proj)
            existing_urls = {e["url"] for e in existing}
            new_entries = []

            # 1. 도메인별 웹 검색
            for domain, queries in cfg["domains"].items():
                for q in queries:
                    log.info(f"  [{domain}] 웹 검색: {q[:50]}")
                    results = _web_search(q, max_results=4)
                    for r in results:
                        if r["url"] and r["url"] not in existing_urls:
                            entry = {
                                "id": url_hash(r["url"]),
                                "url": r["url"],
                                "title": r["title"],
                                "snippet": r["snippet"],
                                "source": r["source"],
                                "domain": domain,
                                "query": q,
                                "collected_at": datetime.now(KST).isoformat(),
                                "used_count": 0,
                                "score": 0.0,
                                "project": proj,
                            }
                            entry["score"] = _score_entry(entry)
                            new_entries.append(entry)
                            existing_urls.add(r["url"])
                    time.sleep(0.5)  # rate limit

            # 2. arXiv 검색
            for q in cfg.get("arxiv_queries", []):
                log.info(f"  [arxiv] {q[:50]}")
                results = _arxiv_search(q, max_results=3)
                for r in results:
                    if r["url"] and r["url"] not in existing_urls:
                        entry = {
                            "id": url_hash(r["url"]),
                            "url": r["url"],
                            "title": r["title"],
                            "snippet": r["snippet"],
                            "source": "arxiv",
                            "domain": "research",
                            "query": q,
                            "published": r.get("published", ""),
                            "collected_at": datetime.now(KST).isoformat(),
                            "used_count": 0,
                            "score": 0.0,
                            "project": proj,
                        }
                        entry["score"] = _score_entry(entry)
                        new_entries.append(entry)
                        existing_urls.add(r["url"])
                time.sleep(0.3)

            # 3. GitHub 검색
            for q in cfg.get("github_queries", []):
                log.info(f"  [github] {q[:50]}")
                results = _github_search(q, max_results=2)
                for r in results:
                    if r["url"] and r["url"] not in existing_urls:
                        entry = {
                            "id": url_hash(r["url"]),
                            "url": r["url"],
                            "title": r["title"],
                            "snippet": r["snippet"],
                            "source": "github",
                            "domain": "tools",
                            "query": q,
                            "stars": r.get("stars", 0),
                            "collected_at": datetime.now(KST).isoformat(),
                            "used_count": 0,
                            "score": 0.0,
                            "project": proj,
                        }
                        entry["score"] = _score_entry(entry)
                        new_entries.append(entry)
                        existing_urls.add(r["url"])
                time.sleep(0.3)

            # 4. RSS 피드
            for feed_url in cfg.get("rss_feeds", []):
                log.info(f"  [rss] {feed_url}")
                results = _rss_fetch(feed_url, max_items=3)
                for r in results:
                    if r["url"] and r["url"] not in existing_urls:
                        entry = {
                            "id": url_hash(r["url"]),
                            "url": r["url"],
                            "title": r["title"],
                            "snippet": r["snippet"],
                            "source": "rss",
                            "domain": "news",
                            "query": feed_url,
                            "collected_at": datetime.now(KST).isoformat(),
                            "used_count": 0,
                            "score": 0.0,
                            "project": proj,
                        }
                        entry["score"] = _score_entry(entry)
                        new_entries.append(entry)
                        existing_urls.add(r["url"])
                time.sleep(0.3)

            # 5. HN 검색 (운영 도메인만)
            if proj == "_nova_ops":
                hn_queries = ["autonomous AI agent 2025", "LLM code review", "AI pipeline testing"]
                for q in hn_queries:
                    results = _hn_search(q, max_results=2)
                    for r in results:
                        if r["url"] and r["url"] not in existing_urls:
                            entry = {
                                "id": url_hash(r["url"]),
                                "url": r["url"],
                                "title": r["title"],
                                "snippet": r["snippet"],
                                "source": "hackernews",
                                "domain": "community",
                                "query": q,
                                "collected_at": datetime.now(KST).isoformat(),
                                "used_count": 0,
                                "score": 0.0,
                                "project": proj,
                            }
                            entry["score"] = _score_entry(entry)
                            new_entries.append(entry)
                            existing_urls.add(r["url"])
                    time.sleep(0.3)

            # 인덱스 저장 (기존 + 신규, score 내림차순)
            all_entries = existing + new_entries
            all_entries.sort(key=lambda x: x.get("score", 0), reverse=True)
            # 최대 500개 유지 (오래된 low-score 제거)
            all_entries = all_entries[:500]
            save_index(proj, all_entries)

            # raw 저장 (신규 항목만)
            raw_d = _raw_dir(proj)
            for entry in new_entries:
                raw_file = raw_d / f"{entry['id']}.json"
                # [R10-CC-004-FIX] raw_file 예외처리 추가
                # [R19-CX-NRC-001-FIX] raw_file 원자적 쓰기 — mkstemp+replace
                try:
                    import tempfile as _tf_raw, os as _os_raw
                    _rfd, _rtmp = _tf_raw.mkstemp(dir=str(raw_d), suffix=".json.tmp")
                    try:
                        with _os_raw.fdopen(_rfd, "w", encoding="utf-8") as _rf:
                            _rfd = -1
                            _rf.write(json.dumps(entry, ensure_ascii=False, indent=2))
                        _os_raw.replace(_rtmp, str(raw_file))
                    except Exception:
                        if _rfd != -1:
                            try: _os_raw.close(_rfd)
                            except OSError: pass
                        try: _os_raw.unlink(_rtmp)
                        except OSError: pass
                        raise
                except OSError as oe:
                    log.warning(f"[collect] raw 파일 저장 실패 ({raw_file}): {oe}")
                    continue

            total_new += len(new_entries)
            log.info(f"[collect] {proj} 완료 — 신규 {len(new_entries)}개 (총 {len(all_entries)}개)")
        finally:
            if _lock_f:
                try:
                    import fcntl as _fcntl2; _fcntl2.flock(_lock_f, _fcntl2.LOCK_UN)
                except Exception: pass
                _lock_f.close()

    print(f"\n[collect] 완료 — 총 신규 리소스 {total_new}개 수집")
    return total_new


def cmd_learn(project: str):
    """상위 리소스 → Claude API로 도메인 지식 추출 → knowledge.md 누적"""
    api_key = _load_api_key()
    if not api_key:
        log.error("[learn] HERMES_API_KEY 없음")
        sys.exit(1)
    if not CLAUDE_API_URL:
        log.error("[learn] 환경변수 CLAUDE_API_URL 미설정 — .env 또는 nova.yaml에서 설정 필요")
        sys.exit(1)

    targets = list(DOMAIN_QUERIES.keys()) if project == "_all" else [project]

    for proj in targets:
        index = load_index(proj)
        if not index:
            log.warning(f"[learn] {proj} 인덱스 없음 — collect 먼저 실행")
            continue

        # 미사용 또는 score 높은 상위 10개 선택
        candidates = [e for e in index if e.get("used_count", 0) < 3]
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        top = candidates[:10]

        if not top:
            log.info(f"[learn] {proj} 학습 대상 없음 (모두 used_count >= 3)")
            continue

        # 리소스 요약 텍스트 구성
        resources_txt = ""
        for i, e in enumerate(top, 1):
            resources_txt += f"{i}. [{e['source']}] {e['title']}\n"
            resources_txt += f"   URL: {e['url']}\n"
            resources_txt += f"   도메인: {e.get('domain','')}\n"
            resources_txt += f"   요약: {e.get('snippet','')[:200]}\n\n"

        # Claude API 호출 — 지식 추출
        cfg = DOMAIN_QUERIES.get(proj, {})
        prompt = f"""당신은 {cfg.get('description', proj)} 전문가입니다.

아래 최신 리소스들을 분석하여 실용적 인사이트를 추출하세요.

=== 리소스 목록 ===
{resources_txt}

다음 형식으로 마크다운 보고서를 작성하세요:

## 핵심 인사이트 (상위 3가지)
각 인사이트는 구체적이고 실행 가능해야 합니다.
- 현재 트렌드와의 연결
- NOVA harness.md에 반영 가능한 구체적 개선 제안

## 전략적 시사점
harness.md Phase 구성 또는 스크립트 로직에 반영할 수 있는 내용

## 주요 발견
숫자/데이터/통계 포함 (가능한 경우)

## 다음 리서치 쿼리 추천 (5개)
현재 갭을 메울 수 있는 구체적 검색어 (수집 루프에 자동 추가됨)
형식: JSON 배열 ["query1", "query2", ...]
"""

        try:
            import urllib.request
            payload = {
                "model": CLAUDE_API_MODEL,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            }
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                CLAUDE_API_URL,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=NOVA_API_TIMEOUT, context=_ssl_ctx()) as resp:
                result = json.loads(resp.read().decode())
            content = result.get("content", [])
            text = content[0].get("text", "") if content else ""

            if text:
                # knowledge.md에 누적 기록
                kp = _knowledge_path(proj)
                now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
                append_txt = f"\n\n---\n## 학습 업데이트 [{now_str}]\n{text}\n"
                # RC-4 fix: 파일 크기 상한 (NOVA_KNOWLEDGE_MAX_KB, 기본 500KB) 초과 시 앞부분 잘라냄
                max_bytes = int(os.environ.get("NOVA_KNOWLEDGE_MAX_KB", "500")) * 1024
                # [R10-CC-003-FIX] knowledge.md 동시 쓰기 락
                try:
                    import fcntl as _fcntl_l
                    _learn_lock_f = open(kp.parent / ".learn.lock", "a")
                    _fcntl_l.flock(_learn_lock_f, _fcntl_l.LOCK_EX)
                except (ImportError, OSError):
                    _learn_lock_f = None
                try:
                    existing = kp.read_text(encoding="utf-8") if kp.exists() else ""
                    combined = existing + append_txt
                    if len(combined.encode("utf-8")) > max_bytes:
                        # 뒤쪽 max_bytes 유지 (최신 데이터 보존) — 경계 줄 맞춤
                        truncated = combined.encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")
                        # 첫 줄 깨짐 방지: 첫 \n 이후부터
                        nl = truncated.find("\n")
                        truncated = "<!-- truncated -->\n" + (truncated[nl+1:] if nl >= 0 else truncated)
                        kp.write_text(truncated, encoding="utf-8")
                        log.info(f"[learn] {proj} knowledge.md 크기 초과 → 앞부분 잘라냄 (max={max_bytes//1024}KB)")
                    else:
                        with open(kp, "a", encoding="utf-8") as f:
                            f.write(append_txt)
                finally:
                    if _learn_lock_f:
                        try:
                            import fcntl as _fcntl_l2; _fcntl_l2.flock(_learn_lock_f, _fcntl_l2.LOCK_UN)
                        except Exception: pass
                        _learn_lock_f.close()
                log.info(f"[learn] {proj} knowledge.md 업데이트 ({len(text)}자)")

                # used_count 업데이트
                for e in top:
                    e["used_count"] = e.get("used_count", 0) + 1

                # 추천 쿼리 자동 추출 + DOMAIN_QUERIES에 동적 추가
                query_match = re.search(r'\["([^"]+)"', text)
                if query_match:
                    try:
                        # JSON 배열 추출
                        # [R10-CC-010-FIX] rfind 안전화 — last_bracket < 0 체크
                        last_bracket = text.rfind(']')
                        if last_bracket < 0:
                            break  # [HIGH-2 FIX] continue→break: proj 루프 skip 방지 (save_index 누락 방지)
                        json_start = text.rfind('[', 0, last_bracket + 1)
                        json_end   = last_bracket + 1
                        if json_start >= 0 and json_end > json_start:
                            new_queries = json.loads(text[json_start:json_end])
                            # _nova_learned_queries.json에 저장
                            lq_path = _resources_dir(proj) / "_learned_queries.json"
                            lq = []
                            if lq_path.exists():
                                try:
                                    lq = json.loads(lq_path.read_text())
                                except json.JSONDecodeError:
                                    # [R10-CC-008-FIX] 파싱 실패 시 경고 로그 후 빈 리스트로 초기화
                                    log.warning(f"[learn] {proj} _learned_queries.json 파싱 실패 — 초기화")
                                    lq = []
                                except Exception:
                                    pass
                            lq_set = set(lq)
                            added = [q for q in new_queries if q not in lq_set]
                            lq.extend(added)
                            # [R10-CC-008-FIX] _learned_queries.json 원자적 쓰기 (tempfile+os.replace)
                            import tempfile as _tf_lq, os as _os_lq
                            _lq_fd, _lq_tmp = _tf_lq.mkstemp(dir=str(lq_path.parent), suffix=".json.tmp")
                            try:
                                with _os_lq.fdopen(_lq_fd, "w", encoding="utf-8") as _lq_f:
                                    _lq_fd = -1
                                    json.dump(lq, _lq_f, ensure_ascii=False, indent=2)
                                _os_lq.replace(_lq_tmp, str(lq_path))
                            except Exception:
                                if _lq_fd != -1:
                                    try: _os_lq.close(_lq_fd)
                                    except OSError: pass
                                try: _os_lq.unlink(_lq_tmp)
                                except OSError: pass
                                raise
                            log.info(f"[learn] {proj} 추천 쿼리 {len(added)}개 저장")
                    except Exception as qe:
                        log.debug(f"[learn] 쿼리 추출 실패: {qe}")

                # 인덱스 저장 (used_count 갱신)
                save_index(proj, index)
                print(f"[learn] {proj} 완료 — knowledge.md 업데이트")
            else:
                log.warning(f"[learn] {proj} Claude 응답 비어있음")

        except Exception as e:
            log.error(f"[learn] {proj} API 오류: {e}", exc_info=True)  # [R10-CC-009-FIX]


def cmd_index(project: str):
    """인덱스 현황 출력 + 점수 재계산"""
    targets = list(DOMAIN_QUERIES.keys()) if project == "_all" else [project]
    for proj in targets:
        index = load_index(proj)
        if not index:
            print(f"[{proj}] 인덱스 없음")
            continue

        # 점수 재계산
        for e in index:
            e["score"] = _score_entry(e)
        index.sort(key=lambda x: x.get("score", 0), reverse=True)
        save_index(proj, index)

        print(f"\n[{proj}] 리소스 인덱스 — {len(index)}개")
        domain_counts = {}
        for e in index:
            d = e.get("domain", "unknown")
            domain_counts[d] = domain_counts.get(d, 0) + 1
        for d, cnt in sorted(domain_counts.items(), key=lambda x: -x[1]):
            print(f"  {d:30s}: {cnt}개")
        print(f"  상위 5개:")
        for e in index[:5]:
            print(f"    [{e.get('score',0):.0f}] {e['title'][:60]} ({e['source']})")


def cmd_cross():
    """크로스 프로젝트 인사이트 생성 — 모든 knowledge.md 통합"""
    api_key = _load_api_key()
    if not api_key:
        log.error("[cross] HERMES_API_KEY 없음")
        sys.exit(1)
    if not CLAUDE_API_URL:
        log.error("[cross] 환경변수 CLAUDE_API_URL 미설정 — .env 또는 nova.yaml에서 설정 필요")
        sys.exit(1)

    combined = ""
    for proj in DOMAIN_QUERIES.keys():
        kp = _knowledge_path(proj)
        if kp.exists():
            txt = kp.read_text(encoding="utf-8")
            # 마지막 3000자만 (최신 인사이트)
            combined += f"\n\n=== [{proj}] ===\n{txt[-3000:]}\n"

    if not combined.strip():
        log.warning("[cross] 수집된 knowledge 없음 — learn 먼저 실행")
        return

    prompt = f"""당신은 NOVA 자율 운영 시스템의 전략 분석가입니다.

아래는 각 프로젝트(blog-pipeline, doosi, unlearning, _nova_ops)의 최신 지식 요약입니다.

{combined[:6000]}

다음을 분석하여 크로스 인사이트를 생성하세요:

## 공통 패턴
모든/다수 프로젝트에서 반복되는 핵심 패턴

## 시너지 기회
A 프로젝트의 학습을 B 프로젝트에 적용할 수 있는 구체적 제안

## NOVA 시스템 개선 제안
harness.md 구조, Phase 설계, 자동화 로직에 반영할 수 있는 통찰

## 우선순위 액션 (다음 evolve 사이클에 반영)
각 프로젝트별 1가지씩 가장 임팩트 높은 개선 액션
"""

    try:
        import urllib.request
        payload = {
            "model": CLAUDE_API_MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            CLAUDE_API_URL, data=data,
            headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=NOVA_API_TIMEOUT, context=_ssl_ctx()) as resp:
            result = json.loads(resp.read().decode())
        content = result.get("content", [])
        text = content[0].get("text", "") if content else ""

        if text:
            cp = _cross_insights_path()
            now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
            with open(cp, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n## 크로스 인사이트 [{now_str}]\n{text}\n")
            log.info(f"[cross] cross_insights.md 업데이트 ({len(text)}자)")
            print(f"[cross] 완료 — {cp}")
        else:
            log.warning("[cross] Claude 응답 비어있음")

    except Exception as e:
        log.error(f"[cross] API 오류: {e}", exc_info=True)  # [R10-CC-009-FIX]


def cmd_status(project: str = None):
    """리소스 현황 요약"""
    targets = list(DOMAIN_QUERIES.keys()) if not project else [project]
    print("\n=== NOVA 리소스 현황 ===\n")
    for proj in targets:
        index = load_index(proj)
        kp = _knowledge_path(proj)
        # [R10-CC-005-FIX] kp.read_text() 읽기 실패 보호
        try:
            k_lines = len(kp.read_text(encoding="utf-8").splitlines()) if kp.exists() else 0
        except OSError:
            k_lines = 0
        lq_path = _resources_dir(proj) / "_learned_queries.json"
        # [R10-CC-005-FIX] lq_path.read_text() 읽기 실패 보호
        try:
            lq_count = len(json.loads(lq_path.read_text())) if lq_path.exists() else 0
        except OSError:
            lq_count = 0
        print(f"  [{proj}]")
        print(f"    리소스 인덱스: {len(index)}개")
        print(f"    knowledge.md : {k_lines}줄")
        print(f"    학습 추천쿼리: {lq_count}개")
        if index:
            unused = len([e for e in index if e.get("used_count", 0) == 0])
            print(f"    미사용 리소스: {unused}개")
    cp = _cross_insights_path()
    if cp.exists():
        print(f"\n  [cross_insights] {len(cp.read_text().splitlines())}줄")
    print()


def get_knowledge_context(project: str, max_chars: int = 2000) -> str:
    """evolve/run_phase에서 knowledge.md 컨텍스트 로드용"""
    kp = _knowledge_path(project)
    if not kp.exists():
        return ""
    txt = kp.read_text(encoding="utf-8")
    # 최신 부분 우선
    return txt[-max_chars:] if len(txt) > max_chars else txt


def get_top_resources(project: str, n: int = 5, domain: str = None) -> list:
    """evolve에서 상위 리소스 참조용"""
    index = load_index(project)
    if domain:
        index = [e for e in index if e.get("domain") == domain]
    return sorted(index, key=lambda x: x.get("score", 0), reverse=True)[:n]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    arg = sys.argv[2] if len(sys.argv) > 2 else "_all"

    if cmd == "collect":
        cmd_collect(arg)
    elif cmd == "learn":
        cmd_learn(arg)
    elif cmd == "index":
        cmd_index(arg)
    elif cmd == "cross":
        cmd_cross()
    elif cmd == "status":
        cmd_status(arg if arg != "_all" else None)
    else:
        print(__doc__)
        sys.exit(1)
