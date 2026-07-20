"""
ownership.py — NOVA Kernel 소유권 규칙 파서

ownership.yaml 을 읽어 에이전트별 경로 접근 권한을 검증한다.
HMG 전용 로직 없음 — yaml 설정만으로 도메인 커스터마이즈 가능.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

import yaml


# ── 기본 yaml 경로 ──────────────────────────────────────────────────────────
_DEFAULT_YAML = Path(__file__).parent / "ownership.yaml"


class OwnershipRules:
    """소유권 규칙 로더 및 검증기.

    yaml_path 가 None 이면 같은 디렉토리의 ownership.yaml 을 사용한다.
    """

    def __init__(self, yaml_path: Optional[str] = None) -> None:
        path = Path(yaml_path) if yaml_path else _DEFAULT_YAML
        if path.exists():
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
            except yaml.YAMLError as exc:          # M-2: YAML 파싱 오류 안전 처리
                import logging
                logging.getLogger(__name__).error(
                    "ownership.yaml 파싱 실패 — 내장 기본값(all-deny)으로 폴백: %s", exc
                )
                raw = _builtin_defaults()
        else:
            # yaml 파일이 없으면 내장 최소 기본값 사용
            raw = _builtin_defaults()

        self._rules: list[dict] = raw.get("rules", [])
        self._defaults: dict = raw.get("defaults", {
            "allow_read": True,
            "allow_unknown": False,
            "auto_assign_agent": True,
        })

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _match_rules(self, path: str, op: str) -> list[dict]:
        """path 와 op 에 매칭되는 규칙 목록 반환 (선언 순서 유지)."""
        matched = []
        for rule in self._rules:
            pattern = rule.get("path_pattern", "")
            ops = rule.get("ops", [])
            if op in ops and _glob_match(pattern, path):
                matched.append(rule)
        return matched

    def _agent_allowed(self, rule: dict, path: str, agent: str) -> bool:
        """단일 규칙에서 agent 가 허용되는지 반환."""
        agents: list = rule.get("agents", [])

        # ["*"] → 모든 에이전트 허용
        if "*" in agents:
            return True

        # 빈 리스트 → 경로에서 에이전트명 추출 (kb/agents/<agent-name>/)
        if not agents:
            extracted = _extract_agent_from_path(path)
            return extracted is not None and extracted == agent

        return agent in agents

    # ── 공개 API ─────────────────────────────────────────────────────────────

    def can_write(self, path: str, agent: str) -> bool:
        """쓰기 권한 확인.

        소유권 규칙 체크. kb/agents/ 경로는 경로에서 agent명 추출.
        규칙 매칭이 없으면 defaults.allow_unknown 값 반환.
        """
        matched = self._match_rules(path, "write")
        if not matched:
            return bool(self._defaults.get("allow_unknown", False))
        # 첫 번째 매칭 규칙에서 허용 여부 결정 (선언 순서 우선)
        return self._agent_allowed(matched[0], path, agent)

    def can_delete(self, path: str, agent: str) -> bool:
        """삭제 권한 확인."""
        matched = self._match_rules(path, "delete")
        if not matched:
            return bool(self._defaults.get("allow_unknown", False))
        return self._agent_allowed(matched[0], path, agent)

    def can_read(self, path: str, agent: str) -> bool:
        """읽기 권한 확인.

        defaults.allow_read 가 True 면 모든 에이전트 허용.
        """
        if self._defaults.get("allow_read", True):
            return True
        matched = self._match_rules(path, "read")
        if not matched:
            return False
        return self._agent_allowed(matched[0], path, agent)

    def assign_agent(self, path: str, requesting_agent: str) -> str:
        """path 에 맞는 소유 agent 반환.

        kb_write 시 agent 컬럼 값 결정에 사용.
        - 빈 agents 규칙(kb/agents/...) → 경로에서 추출
        - 단일 agent 규칙 → 해당 agent 반환
        - 복수 agent 규칙 → requesting_agent 가 포함되면 그대로, 아니면 첫 번째
        - 매칭 없음 → requesting_agent 그대로 반환
        """
        matched = self._match_rules(path, "write")
        if not matched:
            return requesting_agent

        rule = matched[0]
        agents: list = rule.get("agents", [])

        if not agents:  # 빈 리스트 → 경로에서 추출
            extracted = _extract_agent_from_path(path)
            return extracted if extracted else requesting_agent

        if "*" in agents:
            return requesting_agent

        if requesting_agent in agents:
            return requesting_agent

        return agents[0]  # 요청 agent 가 목록에 없으면 첫 번째 소유자


# ── 모듈 레벨 유틸 ───────────────────────────────────────────────────────────

def _glob_match(pattern: str, path: str) -> bool:
    """** (재귀) 와 * (단일 세그먼트) 를 지원하는 glob 매칭.

    fnmatch 는 경로 구분자를 인식하지 못하므로
    ** → 임의 문자열 전체, * → 슬래시 제외 로 처리한다.
    """
    # ** 를 먼저 플레이스홀더로 치환 후 fnmatch 로 처리
    # fnmatch 는 * 가 / 를 포함하지 않음 — ** 를 특수 처리 필요
    regex = _glob_to_regex(pattern)
    return bool(re.fullmatch(regex, path))


def _glob_to_regex(pattern: str) -> str:
    """glob 패턴 → 정규식 변환."""
    parts = pattern.split("**")
    regex_parts = []
    for i, part in enumerate(parts):
        # * → [^/]+ (슬래시 제외 하나 이상)
        escaped = re.escape(part).replace(r"\*", "[^/]*")
        regex_parts.append(escaped)
        if i < len(parts) - 1:
            regex_parts.append(".*")  # ** → 아무 문자열
    return "".join(regex_parts)


def _extract_agent_from_path(path: str) -> Optional[str]:
    """kb/agents/<agent-name>/... 패턴에서 agent 이름 추출."""
    parts = path.split("/")
    # kb/agents/<agent-name>/... 최소 3 세그먼트
    if len(parts) >= 3 and parts[0] == "kb" and parts[1] == "agents":
        return parts[2]
    return None


def _builtin_defaults() -> dict:
    """yaml 파일이 없을 때 사용하는 내장 최소 기본값."""
    return {
        "rules": [
            {"path_pattern": "**", "agents": ["*"], "ops": ["read"]},
        ],
        "defaults": {
            "allow_read": True,
            "allow_unknown": False,
            "auto_assign_agent": True,
        },
    }
