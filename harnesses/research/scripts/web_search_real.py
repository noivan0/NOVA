#!/usr/bin/env python3
"""
research harness - web_search phase 실행 스크립트 v2
DuckDuckGo HTML POST 방식으로 실제 웹 검색 수행
HMG 사내망 SSL 우회 (verify=False)
"""
import sys
import os
import re
import json
import subprocess
from pathlib import Path
from datetime import datetime


def search_ddg_html(topic: str, max_results: int = 8) -> list:
    """DDG HTML POST 방식으로 실제 웹 검색 (SSL 우회)"""
    try:
        import requests
        import warnings
        warnings.filterwarnings("ignore")
        if hasattr(requests.packages, 'urllib3'):
            requests.packages.urllib3.disable_warnings()

        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": topic},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            verify=False,
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        html = resp.text
        # title: <a rel="nofollow" class="result__a" href="URL">TITLE</a>
        hrefs  = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"', html)
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
        bodies = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)

        def clean(s):
            return re.sub(r'<[^>]+>', '', s).strip()

        results = []
        for i in range(min(len(hrefs), len(titles), max_results)):
            body = clean(bodies[i]) if i < len(bodies) else ""
            results.append({
                "title": clean(titles[i]),
                "href":  hrefs[i],
                "body":  body,
            })
        return results
    except Exception as e:
        print(f"[web_search] requests 방식 실패: {e}", file=sys.stderr)
        return []


def search_ddg_subprocess(topic: str, max_results: int = 6) -> list:
    """system python3에서 requests로 검색 (환경 격리 우회)"""
    code = f"""
import requests, re, json, warnings
warnings.filterwarnings('ignore')
try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass
try:
    resp = requests.post(
        'https://html.duckduckgo.com/html/',
        data={{'q': {json.dumps(topic)}}},
        headers={{'User-Agent': 'Mozilla/5.0'}},
        verify=False, timeout=15
    )
    hrefs  = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)"', resp.text)
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
    bodies = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
    def clean(s): return re.sub(r'<[^>]+>','',s).strip()
    results = []
    for i in range(min(len(hrefs), len(titles), {max_results})):
        body = clean(bodies[i]) if i < len(bodies) else ''
        results.append({{'title': clean(titles[i]), 'href': hrefs[i], 'body': body}})
    print(json.dumps(results))
except Exception as e:
    print(json.dumps([]))
"""
    for py in ["/usr/bin/python3", "/usr/local/bin/python3"]:
        try:
            r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout.strip())
                if isinstance(data, list) and data:
                    return data
        except Exception:
            continue
    return []


def main():
    # topic 결정 (환경변수 → argv → workspace/context.json)
    topic = os.environ.get("HARNESS_TOPIC", "")
    if not topic and len(sys.argv) > 1:
        topic = " ".join(sys.argv[1:])
    if not topic:
        workspace = Path(os.environ.get("HARNESS_WORKSPACE", "."))
        ctx_file = workspace / "context.json"
        if ctx_file.exists():
            try:
                ctx = json.loads(ctx_file.read_text())
                topic = ctx.get("topic", "")
            except Exception:
                pass
    if not topic:
        topic = "NOVA autonomous AI agent system architecture"

    print(f"[web_search] 검색 주제: {topic}", file=sys.stderr)

    # 1순위: 직접 requests
    results = search_ddg_html(topic)
    # 2순위: subprocess
    if not results:
        results = search_ddg_subprocess(topic)

    if results:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# Web Search Results: {topic}",
            f"*검색 일시: {now_str} KST | 소스: DuckDuckGo*",
            "",
        ]
        for i, r in enumerate(results, 1):
            lines.append(f"## [{i}] {r.get('title', '(제목 없음)')}")
            lines.append(f"URL: {r.get('href', '')}")
            body = r.get("body", "")
            if body:
                lines.append(body)
            lines.append("")
        output = "\n".join(lines)
        print(f"[web_search] {len(results)}건 실제 웹 검색 완료", file=sys.stderr)
    else:
        output = (
            f"# Web Search Results: {topic}\n\n"
            "[web_search] DDG 검색 결과 없음 — KB 컨텍스트만 활용합니다.\n"
            f"(HMG 사내망 SSL 제한 가능성 — 주제: {topic})"
        )
        print("[web_search] 최종 검색 실패 — fallback to KB only", file=sys.stderr)

    # 결과 파일 저장
    workspace = Path(os.environ.get("HARNESS_WORKSPACE", "."))
    out_file = workspace / "web_research.md"
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(output, encoding="utf-8")
        print(f"[web_search] → {out_file}", file=sys.stderr)
    except Exception as e:
        print(f"[web_search] 파일 저장 실패: {e}", file=sys.stderr)

    print(output)


if __name__ == "__main__":
    main()
