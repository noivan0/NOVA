#!/usr/bin/env python3
"""
NOVA 리소스 자율 업데이트 스크립트
- RSS 피드 감시 → 새 항목 감지 → KB 업데이트 → Telegram 알림
- 실행: python3 nova_resource_updater.py [--domain seo|marketing|dev|all]
"""

import sys
import os
import json
import argparse
import subprocess
import datetime
import tempfile
from pathlib import Path

# [R13-CC-001-FIX] /root 하드코딩 제거 → 환경변수 HERMES_HOME 우선, 없으면 ~/.hermes fallback
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
KB_PATH = _HERMES_HOME / "kb" / "projects" / "nova-resources.md"
LOG_PATH = _HERMES_HOME / "kb" / "log.md"
CACHE_PATH = _HERMES_HOME / "cache" / "nova_resources_cache.json"

# 도메인별 RSS 피드
RSS_FEEDS = {
    "seo": [
        {
            "name": "Google Search Central Blog",
            "url": "https://feeds.feedburner.com/blogspot/amDG",
            "domain": "SEO",
            "keywords": ["core update", "ranking", "crawl", "index", "search"]
        },
    ],
    "marketing": [
        {
            "name": "HubSpot Marketing",
            "url": "https://blog.hubspot.com/marketing/rss.xml",
            "domain": "마케팅",
            "keywords": ["carousel", "reels", "content", "instagram", "engagement"]
        },
        {
            "name": "Sprout Social",
            "url": "https://sproutsocial.com/insights/feed/",
            "domain": "SNS",
            "keywords": ["carousel", "engagement", "instagram", "benchmark"]
        },
        {
            "name": "Hootsuite",
            "url": "https://blog.hootsuite.com/feed/",
            "domain": "SNS전략",
            "keywords": ["reels", "shorts", "trends", "algorithm"]
        },
    ],
    "dev": [
        {
            "name": "OWASP News",
            "url": "https://owasp.org/feed.xml",
            "domain": "보안",
            "keywords": ["vulnerability", "security", "owasp", "cve"]
        },
    ]
}

def load_cache():
    # [R13-CC-002-FIX] JSONDecodeError recovery — 손상된 캐시 파일 시 {} 반환
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[캐시 경고] 캐시 손상/읽기 실패, 초기화: {e}", file=sys.stderr)
    return {}

def save_cache(cache):
    # [R13-CC-003-FIX] 원자적 쓰기 — mkstemp+replace로 캐시 손상 방지
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _fd, _tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), suffix=".json.tmp")
    try:
        with os.fdopen(_fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(_tmp, CACHE_PATH)
    except Exception:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise

def fetch_rss(url):
    """RSS 피드 가져오기 (feedparser 사용)"""
    try:
        result = subprocess.run(
            ["python3", "-c", f"""
import feedparser, json, sys
feed = feedparser.parse('{url}')
items = []
for entry in feed.entries[:5]:
    items.append({{
        'title': entry.get('title', ''),
        'link': entry.get('link', ''),
        'published': entry.get('published', ''),
        'summary': entry.get('summary', '')[:300]
    }})
print(json.dumps(items, ensure_ascii=False))
"""],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        print(f"[RSS 오류] {url}: {e}", file=sys.stderr)
    return []

def check_new_items(feed_config, cache):
    """새 항목 감지"""
    items = fetch_rss(feed_config["url"])
    cache_key = feed_config["url"]
    cached_links = set(cache.get(cache_key, []))
    
    new_items = []
    all_links = []
    for item in items:
        link = item.get("link", "")
        all_links.append(link)
        if link and link not in cached_links:
            # 키워드 필터링
            title_lower = item.get("title", "").lower()
            summary_lower = item.get("summary", "").lower()
            if any(kw in title_lower or kw in summary_lower for kw in feed_config["keywords"]):
                new_items.append(item)
    
    # 캐시 업데이트
    cache[cache_key] = list(set(cached_links) | set(all_links))
    return new_items

def append_to_kb(domain, new_items):
    """KB에 새 항목 추가"""
    if not new_items or not KB_PATH.exists():
        return
    
    today = datetime.date.today().isoformat()
    append_text = f"\n\n### {today} 자동 업데이트 — {domain}\n\n"
    for item in new_items:
        append_text += f"- [{item['title']}]({item['link']}) ({item.get('published', '')[:10]})\n"
        if item.get('summary'):
            append_text += f"  > {item['summary'][:200]}\n"
    
    # [R13-CC-004-FIX] fcntl LOCK_EX — 동시 실행 시 KB 파일 충돌 방지
    with open(KB_PATH, "a", encoding="utf-8") as f:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        f.write(append_text)

def log_update(domain, count):
    """log.md에 기록"""
    today = datetime.date.today().isoformat()
    log_entry = f"## [{today}] nova-resources 자율 업데이트 | {domain} {count}개 신규\n"
    # [R13-CC-004-FIX] fcntl LOCK_EX — 동시 실행 시 log.md 충돌 방지
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        f.write(log_entry)

def send_telegram_summary(summary_lines):
    """헤르메스 Telegram 알림 (옵션)"""
    if not summary_lines:
        return
    msg = "NOVA 리소스 자율 업데이트 완료\n" + "\n".join(summary_lines)
    # Telegram 발송은 Hermes 크론 deliver를 통해 자동 처리
    print(msg)

def main():
    parser = argparse.ArgumentParser(description="NOVA 리소스 자율 업데이트")
    parser.add_argument("--domain", default="all", choices=["seo", "marketing", "dev", "all"])
    parser.add_argument("--dry-run", action="store_true", help="실제 KB 수정 없이 결과만 출력")
    args = parser.parse_args()

    cache = load_cache()
    summary = []

    domains = list(RSS_FEEDS.keys()) if args.domain == "all" else [args.domain]
    
    for domain in domains:
        feeds = RSS_FEEDS.get(domain, [])
        domain_new = []
        
        for feed in feeds:
            print(f"[{domain}] {feed['name']} 확인 중...", file=sys.stderr)
            new_items = check_new_items(feed, cache)
            if new_items:
                domain_new.extend(new_items)
                print(f"  → {len(new_items)}개 신규 항목 발견", file=sys.stderr)
        
        if domain_new:
            if not args.dry_run:
                # [C-3 fix] feed 변수가 미정의일 수 있음 → domain 변수 사용
                append_to_kb(domain, domain_new)
                log_update(domain, len(domain_new))
            summary.append(f"[{domain}] {len(domain_new)}개 신규")
        else:
            print(f"[{domain}] 신규 항목 없음", file=sys.stderr)

    if not args.dry_run:
        save_cache(cache)

    result = {
        "timestamp": datetime.datetime.now().isoformat(),
        "domains_checked": domains,
        "summary": summary,
        "status": "completed"
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    send_telegram_summary(summary)

if __name__ == "__main__":
    main()
