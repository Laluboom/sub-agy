"""Quota query: parse Antigravity usage envelope into normalized groups."""

from __future__ import annotations

import json
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass

from .config import AccountConfig, Config, agy_command

_REFRESH_RE = re.compile(r"refresh in (.+?)\.?$", re.IGNORECASE)

_WEEKLY_NAMES = {"weekly limit remaining", "weekly"}
_FIVE_HOUR_NAMES = {"five hour limit remaining", "five hour", "5h"}

_GROUP_NAME_ZH = {
    "Gemini Models": "Gemini 模型",
    "Claude and GPT models": "Claude/GPT 模型",
}


def _humanize_reset_zh(reset_in: str | None) -> str | None:
    """Convert English reset_in string to compact Chinese.

    Examples:
        '6 days, 9 hours' -> '6天9小时'
        '4 hours, 28 minutes' -> '4小时28分'
        '32 minutes' -> '32分钟'
    """
    if not reset_in or reset_in.strip() in ("", "-"):
        return None

    text = reset_in.strip()
    day_match = re.search(r"(\d+)\s*days?", text, re.IGNORECASE)
    hour_match = re.search(r"(\d+)\s*hours?", text, re.IGNORECASE)
    min_match = re.search(r"(\d+)\s*minutes?", text, re.IGNORECASE)
    sec_match = re.search(r"(\d+)\s*seconds?", text, re.IGNORECASE)

    parts: list[str] = []
    if day_match:
        parts.append(f"{day_match.group(1)}天")
    if hour_match:
        parts.append(f"{hour_match.group(1)}小时")
    if min_match:
        m_val = min_match.group(1)
        if day_match or hour_match:
            parts.append(f"{m_val}分")
        else:
            parts.append(f"{m_val}分钟")
    if sec_match:
        parts.append(f"{sec_match.group(1)}秒")

    if not parts:
        return None
    return "".join(parts)


def format_oneline(data: dict) -> str:
    """Format normalized quota data into a compact Chinese sentence."""
    groups = data.get("groups", [])
    if not groups:
        return ""

    group_sentences: list[str] = []
    for group in groups:
        raw_name = group.get("name", "Unknown")
        group_name = _GROUP_NAME_ZH.get(raw_name, raw_name)

        buckets_by_window: dict[str, dict] = {}
        for bucket in group.get("buckets", []):
            window = bucket.get("window")
            if window and window not in buckets_by_window:
                buckets_by_window[window] = bucket

        clauses: list[str] = []
        for target_window, label in [("5h", "5h"), ("weekly", "7d")]:
            bucket = buckets_by_window.get(target_window)
            if not bucket:
                continue
            pct = bucket.get("remaining_pct", 0.0)
            reset_zh = _humanize_reset_zh(bucket.get("reset_in"))
            if reset_zh:
                clauses.append(f"{label} 限额剩余 {pct:.1f}%（{reset_zh}后重置）")
            else:
                clauses.append(f"{label} 限额剩余 {pct:.1f}%")

        if clauses:
            group_sentences.append(f"{group_name}：{'，'.join(clauses)}")

    return "；".join(group_sentences)



def _window_from_text(name: str) -> str:
    lowered = name.strip().lower()
    if lowered in _WEEKLY_NAMES:
        return "weekly"
    if lowered in _FIVE_HOUR_NAMES:
        return "5h"
    # Fallback heuristic: if it contains "week" or "hour".
    if "week" in lowered:
        return "weekly"
    if "hour" in lowered or "5h" in lowered:
        return "5h"
    return lowered


def _extract_reset_in(description: str | None) -> str | None:
    if not description:
        return None
    match = _REFRESH_RE.search(description)
    if not match:
        return None
    return match.group(1).strip()


def _normalize_structured(envelope: dict) -> dict:
    raw_groups = envelope.get("command", {}).get("data", {}).get("groups", [])
    groups = []
    for raw_group in raw_groups:
        buckets = []
        for raw_bucket in raw_group.get("buckets", []):
            fraction = raw_bucket.get("remaining_fraction")
            if fraction is None:
                continue
            try:
                remaining_pct = round(float(fraction) * 100, 1)
            except (TypeError, ValueError):
                continue
            buckets.append(
                {
                    "window": raw_bucket.get("window", "unknown"),
                    "remaining_pct": remaining_pct,
                    "reset_time": raw_bucket.get("reset_time"),
                    "reset_in": _extract_reset_in(raw_bucket.get("description")),
                }
            )
        groups.append({"name": raw_group.get("name", "Unknown"), "buckets": buckets})
    return {"ok": True, "source": "structured", "groups": groups}


def _parse_percent(text: str) -> float | None:
    text = text.strip()
    if text.endswith("%"):
        text = text[:-1]
    try:
        return round(float(text), 1)
    except ValueError:
        return None


def _normalize_text_fallback(response: str) -> dict:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for line in response.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            parts = line.split("  ")
        if len(parts) < 4:
            continue
        group_name, bucket_name, pct_text, reset_time = parts[0], parts[1], parts[2], parts[3]
        remaining_pct = _parse_percent(pct_text)
        if remaining_pct is None:
            continue
        window = _window_from_text(bucket_name)
        bucket = {
            "window": window,
            "remaining_pct": remaining_pct,
            "reset_time": reset_time.strip() or None,
            "reset_in": None,
        }
        groups.setdefault(group_name.strip(), []).append(bucket)
    return {
        "ok": True,
        "source": "text_fallback",
        "groups": [{"name": name, "buckets": buckets} for name, buckets in groups.items()],
    }


def _parse_envelope(stdout: str) -> dict:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON from agy: {exc}") from exc

    if not isinstance(envelope, dict):
        raise ValueError("unexpected agy response type")

    status = envelope.get("status")
    if status != "SUCCESS":
        error = envelope.get("error") or f"agy returned status {status}"
        raise RuntimeError(error)

    if envelope.get("command", {}).get("data", {}).get("groups"):
        return _normalize_structured(envelope)

    response = envelope.get("response")
    if isinstance(response, str) and response.strip():
        return _normalize_text_fallback(response)

    raise ValueError("agy usage response missing structured data and text response")


def fetch_quota(config: Config | AccountConfig | str, timeout: float = 75.0) -> dict:
    """Run agy usage query and return normalized quota data.

    Raises:
        FileNotFoundError: agy binary not found (maps to exit 127).
        RuntimeError/ValueError: other failures (maps to exit 1).
    """
    argv = [
        *agy_command(config),
        "-p",
        "/usage",
        "--output-format",
        "json",
        "--print-timeout",
        "60s",
    ]

    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"agy not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"agy quota query timed out after {timeout}s") from exc

    if result.returncode != 0:
        err = result.stderr.strip() or f"agy exited {result.returncode}"
        raise RuntimeError(err)

    return _parse_envelope(result.stdout)


@dataclass(frozen=True)
class AccountQuotaInfo:
    account: AccountConfig
    ok: bool
    five_hour_pct: float | None = None
    weekly_pct: float | None = None
    error: str | None = None
    raw_data: dict | None = None

    @property
    def bottleneck_pct(self) -> float | None:
        if self.five_hour_pct is not None and self.weekly_pct is not None:
            return min(self.five_hour_pct, self.weekly_pct)
        return None


def extract_gemini_headroom(quota_data: dict) -> tuple[float | None, float | None]:
    """Extract (five_hour_pct, weekly_pct) for Gemini models from normalized quota data."""
    groups = quota_data.get("groups", [])
    # First search for group with 'gemini' in name
    for group in groups:
        name = group.get("name", "").lower()
        if "gemini" in name:
            five_h = None
            weekly = None
            for bucket in group.get("buckets", []):
                window = bucket.get("window")
                pct = bucket.get("remaining_pct")
                if pct is not None:
                    if window == "5h" and five_h is None:
                        five_h = float(pct)
                    elif window == "weekly" and weekly is None:
                        weekly = float(pct)
            if five_h is not None and weekly is not None:
                return five_h, weekly

    # Fallback to any group that has both 5h and weekly buckets
    for group in groups:
        five_h = None
        weekly = None
        for bucket in group.get("buckets", []):
            window = bucket.get("window")
            pct = bucket.get("remaining_pct")
            if pct is not None:
                if window == "5h" and five_h is None:
                    five_h = float(pct)
                elif window == "weekly" and weekly is None:
                    weekly = float(pct)
        if five_h is not None and weekly is not None:
            return five_h, weekly

    return None, None


def query_account_quota(account: AccountConfig, timeout: float = 75.0) -> AccountQuotaInfo:
    """Fetch quota for a specific account and parse Gemini headroom."""
    try:
        data = fetch_quota(account, timeout=timeout)
        five_h, weekly = extract_gemini_headroom(data)
        if five_h is None or weekly is None:
            return AccountQuotaInfo(
                account=account,
                ok=False,
                error="missing 5h or weekly Gemini quota in agy response",
                raw_data=data,
            )
        return AccountQuotaInfo(
            account=account,
            ok=True,
            five_hour_pct=five_h,
            weekly_pct=weekly,
            raw_data=data,
        )
    except Exception as exc:
        return AccountQuotaInfo(
            account=account,
            ok=False,
            error=str(exc),
        )


def select_account(
    config: Config,
    requested_name: str | None = None,
    timeout: float = 75.0,
) -> tuple[AccountConfig, dict]:
    """Select the best account based on configuration and Gemini quota headroom.

    Returns:
        (selected_account, metadata_dict)
    Raises:
        ValueError: if requested_name is unknown
        RuntimeError: if every configured account has low quota or quota query fails
    """
    if not config.accounts:
        if requested_name:
            if requested_name == "default":
                return config.default_account(), {"reason": "default"}
            raise ValueError(f"unknown account '{requested_name}': no accounts configured")
        return config.default_account(), {"reason": "default"}

    if requested_name:
        acc = config.get_account(requested_name)
        if acc is None:
            valid = ", ".join(f"'{a.name}'" for a in config.accounts)
            raise ValueError(f"unknown account '{requested_name}'. Configured accounts: {valid}")
        return acc, {"reason": "explicit", "account": acc.name}

    # Auto-selection across configured accounts
    infos: list[AccountQuotaInfo] = []
    for acc in config.accounts:
        infos.append(query_account_quota(acc, timeout=timeout))

    # Evaluate headroom against threshold
    eligible: list[tuple[AccountQuotaInfo, float]] = []
    reasons: list[str] = []

    for info in infos:
        thresh = (
            info.account.low_quota_threshold
            if info.account.low_quota_threshold is not None
            else config.low_quota_threshold
        )
        if not info.ok:
            reasons.append(f"account '{info.account.name}': quota query failed ({info.error})")
            continue

        if info.five_hour_pct >= thresh and info.weekly_pct >= thresh:
            eligible.append((info, thresh))
        else:
            low_parts = []
            if info.five_hour_pct < thresh:
                low_parts.append(f"5h remaining {info.five_hour_pct:.1f}% < {thresh:.1f}%")
            if info.weekly_pct < thresh:
                low_parts.append(f"weekly remaining {info.weekly_pct:.1f}% < {thresh:.1f}%")
            reasons.append(f"account '{info.account.name}': below threshold ({', '.join(low_parts)})")

    if eligible:
        # Choose the account with the best bottleneck headroom; ties broken by total headroom, then config order
        best_info, thresh = max(
            eligible,
            key=lambda item: (
                item[0].bottleneck_pct if item[0].bottleneck_pct is not None else 0.0,
                (item[0].five_hour_pct or 0.0) + (item[0].weekly_pct or 0.0),
            ),
        )
        return best_info.account, {
            "reason": "quota_selected",
            "account": best_info.account.name,
            "five_hour_pct": best_info.five_hour_pct,
            "weekly_pct": best_info.weekly_pct,
        }

    # No eligible accounts
    error_msg = (
        "no configured account has sufficient Gemini quota:\n  • "
        + "\n  • ".join(reasons)
        + "\nUse `run --account NAME` to select an account deliberately."
    )
    raise RuntimeError(error_msg)
