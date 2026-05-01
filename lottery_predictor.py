#!/usr/bin/env python3
"""
Daily lottery data collector and statistical predictor for:
- China Welfare Lottery 3D
- China Sports Lottery Pailie 3
- China Sports Lottery Pailie 5

This is a statistical logging tool. Lottery drawings are random; generated
numbers are not guarantees or investment advice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import http.server
import itertools
import json
import math
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
DOCS_DIR = ROOT / "docs"
CONFIG_PATH = ROOT / "config.json"
WEB_REFRESH_LOCK = threading.Lock()
LAST_WEB_REFRESH_AT: dt.datetime | None = None


LOTTERIES = {
    "fc3d": {
        "name": "中国福利彩票 3D",
        "digits": 3,
        "sources": [
            {
                "type": "touch_history",
                "url": "https://touch.17500.cn/award/history/lotid/3d.html",
            },
            {
                "type": "plain_text",
                "url": "http://data.17500.cn/3d_asc.txt",
            },
            {
                "type": "cwl_json",
                "url": "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice?name=3d&issueCount=200",
            },
            {
                "type": "html_table",
                "url": "https://www.cwl.gov.cn/ygkj/wqkjgg/3d/",
            },
        ],
    },
    "pls": {
        "name": "中国体育彩票 排列三",
        "digits": 3,
        "sources": [
            {
                "type": "touch_history",
                "url": "https://touch.17500.cn/award/history/lotid/pl3.html",
            },
            {
                "type": "plain_text",
                "url": "http://data.17500.cn/pl3_asc.txt",
            },
            {
                "type": "lottery_gov_history",
                "url": "https://www.lottery.gov.cn/historykj/history.jspx?_ltype=pls",
            },
            {
                "type": "html_table",
                "url": "https://m.lottery.gov.cn/zst/pls/",
            },
        ],
    },
    "plw": {
        "name": "中国体育彩票 排列五",
        "digits": 5,
        "sources": [
            {
                "type": "touch_history",
                "url": "https://touch.17500.cn/award/history/lotid/pl5.html",
            },
            {
                "type": "plain_text",
                "url": "http://data.17500.cn/pl5_asc.txt",
            },
            {
                "type": "lottery_gov_history",
                "url": "https://www.lottery.gov.cn/historykj/history.jspx?_ltype=plw",
            },
            {
                "type": "html_table",
                "url": "https://m.lottery.gov.cn/zst/plw/",
            },
        ],
    },
}


DEFAULT_CONFIG = {
    "predict_time": "20:00",
    "post_draw_time": "22:00",
    "timezone_note": "Use the host machine local timezone.",
    "history_limit": 220,
    "candidate_count": 20,
    "backtest_window": 60,
    "request_timeout_seconds": 8,
    "user_agent": "Mozilla/5.0 lottery-statistics-bot/1.0",
    "lotteries": ["fc3d", "pls", "plw"],
    "weights": {
        "frequency": 0.34,
        "recency": 0.28,
        "omission": 0.22,
        "transition": 0.16,
    },
    "model_weights": {
        "legacy": 0.30,
        "markov": 0.30,
        "bayes": 0.24,
        "shape": 0.16
    },
    "signal_weight": 0.18,
    "web_refresh_predict_minutes": 0,
    "web_refresh_post_draw_always": True,
}


SAMPLE_HISTORY = {
    "fc3d": [
        ("2026101", "2026-04-22", "058"),
        ("2026102", "2026-04-23", "314"),
        ("2026103", "2026-04-24", "769"),
        ("2026104", "2026-04-25", "206"),
        ("2026105", "2026-04-26", "482"),
        ("2026106", "2026-04-27", "137"),
        ("2026107", "2026-04-28", "590"),
        ("2026108", "2026-04-29", "826"),
        ("2026109", "2026-04-30", "641"),
        ("2026110", "2026-05-01", "275"),
    ],
    "pls": [
        ("2026101", "2026-04-22", "927"),
        ("2026102", "2026-04-23", "164"),
        ("2026103", "2026-04-24", "503"),
        ("2026104", "2026-04-25", "788"),
        ("2026105", "2026-04-26", "219"),
        ("2026106", "2026-04-27", "456"),
        ("2026107", "2026-04-28", "830"),
        ("2026108", "2026-04-29", "372"),
        ("2026109", "2026-04-30", "695"),
        ("2026110", "2026-05-01", "041"),
    ],
    "plw": [
        ("2026101", "2026-04-22", "92713"),
        ("2026102", "2026-04-23", "16480"),
        ("2026103", "2026-04-24", "50326"),
        ("2026104", "2026-04-25", "78841"),
        ("2026105", "2026-04-26", "21975"),
        ("2026106", "2026-04-27", "45603"),
        ("2026107", "2026-04-28", "83062"),
        ("2026108", "2026-04-29", "37294"),
        ("2026109", "2026-04-30", "69518"),
        ("2026110", "2026-05-01", "04157"),
    ],
}


@dataclass(frozen=True)
class Draw:
    issue: str
    date: str
    numbers: tuple[int, ...]


class TextTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"td", "th"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell:
            text = " ".join("".join(self._cell_parts).split())
            self._row.append(text)
            self._in_cell = False
        elif tag == "tr":
            if any(self._row):
                self.rows.append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    merged["weights"] = {**DEFAULT_CONFIG["weights"], **config.get("weights", {})}
    merged["model_weights"] = {**DEFAULT_CONFIG["model_weights"], **config.get("model_weights", {})}
    return merged


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def fetch_text(url: str, timeout: int, user_agent: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Referer": urllib.parse.urljoin(url, "/"),
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
        encoding = "utf-8"
        match = re.search(r"charset=([\w-]+)", content_type, re.I)
        if match:
            encoding = match.group(1)
        try:
            return raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            return raw.decode("gb18030", errors="replace")


def parse_digits(value: Any, digits: int) -> tuple[int, ...] | None:
    if value is None:
        return None
    text = str(value)
    found = re.findall(r"\d", text)
    if len(found) < digits:
        return None
    return tuple(int(x) for x in found[:digits])


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return text[:10]


def parse_cwl_json(text: str, digits: int) -> list[Draw]:
    data = json.loads(text)
    items = data.get("result") or data.get("data") or []
    draws: list[Draw] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("code") or item.get("issue") or item.get("expect") or "").strip()
        date = normalize_date(item.get("date") or item.get("openTime") or item.get("day"))
        numbers = parse_digits(item.get("red") or item.get("number") or item.get("openCode"), digits)
        if issue and numbers and len(numbers) == digits:
            draws.append(Draw(issue=issue, date=date, numbers=numbers))
    return sorted(unique_draws(draws), key=lambda x: x.issue)


def parse_lottery_gov_history(text: str, digits: int) -> list[Draw]:
    # The official sporttery history page has changed shapes over time. This
    # parser accepts JSON-like payloads embedded in the page and ordinary tables.
    draws = parse_embedded_draws(text, digits)
    if draws:
        return sorted(unique_draws(draws), key=lambda x: x.issue)
    return parse_html_table(text, digits)


def parse_plain_text(text: str, digits: int) -> list[Draw]:
    draws: list[Draw] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        issue_match = re.search(r"\b(\d{5,})\b", line)
        if not issue_match:
            continue
        tail = line[issue_match.end() :]
        date_match = re.search(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", line)
        compact_match = re.search(rf"\b(\d{{{digits}}})\b", tail)
        numbers = parse_digits(compact_match.group(1), digits) if compact_match else None
        if not numbers:
            tail_digits = re.findall(r"\d", tail)
            if len(tail_digits) >= digits:
                numbers = tuple(int(x) for x in tail_digits[-digits:])
        if numbers:
            draws.append(
                Draw(
                    issue=issue_match.group(1),
                    date=normalize_date(date_match.group(0) if date_match else ""),
                    numbers=numbers,
                )
            )
    return sorted(unique_draws(draws), key=lambda x: x.issue)


def parse_touch_history(text: str, digits: int) -> list[Draw]:
    clean = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    clean = " ".join(clean.split())
    draws: list[Draw] = []

    pattern = re.compile(
        rf"(\d{{5,8}})\s*期\s*复制\s*"
        rf"(?:(20\d{{2}}[-/.年]\d{{1,2}}[-/.月]\d{{1,2}}|昨天)\s*)?"
        rf"\d{{1,2}}:\d{{2}}\s+"
        rf"((?:\d\s+){{{digits - 1}}}\d)",
        re.S,
    )
    current_year = str(dt.datetime.now().year)
    for match in pattern.finditer(clean):
        issue = match.group(1)
        date_text = match.group(2) or ""
        if date_text == "昨天":
            date_text = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        elif re.fullmatch(r"\d{2}-\d{2}", date_text):
            date_text = f"{current_year}-{date_text}"
        numbers = parse_digits(match.group(3), digits)
        if issue and numbers:
            draws.append(Draw(issue=issue, date=normalize_date(date_text), numbers=numbers))
    return sorted(unique_draws(draws), key=lambda x: x.issue)


def parse_embedded_draws(text: str, digits: int) -> list[Draw]:
    draws: list[Draw] = []
    for match in re.finditer(r"\{[^{}]*(?:draw|lottery|issue|code|number|result)[^{}]*\}", text, re.I):
        blob = match.group(0)
        issue_match = re.search(r'"?(?:issue|code|lotteryDrawNum|drawNo)"?\s*:\s*"?(\d{5,})"?', blob, re.I)
        num_match = re.search(r'"?(?:number|openCode|lotteryDrawResult|result)"?\s*:\s*"?([0-9,\s|]+)"?', blob, re.I)
        date_match = re.search(r'"?(?:date|openTime|lotteryDrawTime)"?\s*:\s*"?(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})"?', blob, re.I)
        if not issue_match or not num_match:
            continue
        numbers = parse_digits(num_match.group(1), digits)
        if numbers:
            draws.append(Draw(issue=issue_match.group(1), date=normalize_date(date_match.group(1) if date_match else ""), numbers=numbers))
    return draws


def parse_html_table(text: str, digits: int) -> list[Draw]:
    parser = TextTableParser()
    parser.feed(text)
    draws: list[Draw] = []
    for row in parser.rows:
        joined = " ".join(row)
        issue_match = re.search(r"\b(\d{5,})\b", joined)
        date_match = re.search(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", joined)
        if not issue_match:
            continue
        number_chunks = []
        for cell in row:
            digits_in_cell = re.findall(r"\d", cell)
            if len(digits_in_cell) == digits:
                number_chunks.append(cell)
        numbers = parse_digits(number_chunks[-1] if number_chunks else joined, digits)
        if numbers:
            draws.append(
                Draw(
                    issue=issue_match.group(1),
                    date=normalize_date(date_match.group(0) if date_match else ""),
                    numbers=numbers,
                )
            )
    return sorted(unique_draws(draws), key=lambda x: x.issue)


def unique_draws(draws: Iterable[Draw]) -> list[Draw]:
    by_issue: dict[str, Draw] = {}
    for draw in draws:
        by_issue[draw.issue] = draw
    return list(by_issue.values())


def data_file(lottery_key: str) -> Path:
    return DATA_DIR / f"{lottery_key}.csv"


def read_history(lottery_key: str) -> list[Draw]:
    path = data_file(lottery_key)
    if not path.exists():
        return []
    draws: list[Draw] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            numbers = parse_digits(row.get("numbers", ""), int(row.get("digits", "3")))
            if numbers:
                draws.append(Draw(row["issue"], row.get("date", ""), numbers))
    draws = sorted(unique_draws(draws), key=lambda x: x.issue)
    return draws if is_valid_history(draws, LOTTERIES[lottery_key]["digits"]) else []


def is_valid_history(draws: list[Draw], digits: int) -> bool:
    if not draws:
        return False
    if any(len(draw.numbers) != digits for draw in draws):
        return False
    if len(draws) >= 20:
        latest = draws[-20:]
        repeated = max(
            sum(1 for draw in latest if draw.numbers == numbers)
            for numbers in {draw.numbers for draw in latest}
        )
        if repeated >= 12:
            return False
        if digits == 5 and sum(1 for draw in latest if draw.numbers == (0, 0, 0, 0, 0)) >= 3:
            return False
    return True


def write_history(lottery_key: str, draws: list[Draw]) -> None:
    path = data_file(lottery_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["issue", "date", "numbers", "digits"])
        writer.writeheader()
        for draw in sorted(unique_draws(draws), key=lambda x: x.issue):
            writer.writerow(
                {
                    "issue": draw.issue,
                    "date": draw.date,
                    "numbers": "".join(str(x) for x in draw.numbers),
                    "digits": len(draw.numbers),
                }
            )


def sample_history(lottery_key: str) -> list[Draw]:
    rows = SAMPLE_HISTORY[lottery_key]
    return [
        Draw(issue=issue, date=date, numbers=tuple(int(x) for x in number))
        for issue, date, number in rows
    ]


def collect_lottery(lottery_key: str, config: dict[str, Any]) -> tuple[list[Draw], str]:
    if config.get("offline"):
        local = read_history(lottery_key)
        return (local or sample_history(lottery_key)), "offline-cache"

    spec = LOTTERIES[lottery_key]
    errors = []
    for source in spec["sources"]:
        try:
            text = fetch_text(source["url"], config["request_timeout_seconds"], config["user_agent"])
            if source["type"] == "cwl_json":
                draws = parse_cwl_json(text, spec["digits"])
            elif source["type"] == "lottery_gov_history":
                draws = parse_lottery_gov_history(text, spec["digits"])
            elif source["type"] == "touch_history":
                draws = parse_touch_history(text, spec["digits"])
            elif source["type"] == "plain_text":
                draws = parse_plain_text(text, spec["digits"])
            else:
                draws = parse_html_table(text, spec["digits"])
            if is_valid_history(draws, spec["digits"]):
                local = read_history(lottery_key)
                merged = sorted(unique_draws([*local, *draws]), key=lambda x: x.issue)
                merged = merged[-int(config["history_limit"]) :]
                write_history(lottery_key, merged)
                return merged, source["url"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{source['url']}: {exc}")
    local = read_history(lottery_key)
    if local:
        return local, "local-cache"
    raise RuntimeError(f"{spec['name']} has no usable data. Errors: {'; '.join(errors)}")


def position_stats(draws: list[Draw], digits: int, weights: dict[str, float]) -> list[list[float]]:
    total = max(1, len(draws))
    stats: list[list[float]] = []
    previous = draws[-1].numbers if draws else tuple([0] * digits)
    for pos in range(digits):
        freq = [0.0] * 10
        recency = [0.0] * 10
        omission = [0.0] * 10
        transition = [0.0] * 10

        for idx, draw in enumerate(draws):
            n = draw.numbers[pos]
            freq[n] += 1.0
            recency[n] += math.exp((idx - total + 1) / 24.0)

        for n in range(10):
            last_seen = None
            for offset, draw in enumerate(reversed(draws)):
                if draw.numbers[pos] == n:
                    last_seen = offset
                    break
            omission[n] = min((last_seen if last_seen is not None else total) / 20.0, 2.0)

        prev_digit = previous[pos]
        for before, after in zip(draws[:-1], draws[1:]):
            if before.numbers[pos] == prev_digit:
                transition[after.numbers[pos]] += 1.0

        combined = normalize_scores(freq)
        rec = normalize_scores(recency)
        omi = normalize_scores(omission)
        tra = normalize_scores(transition)
        probs = [
            weights["frequency"] * combined[n]
            + weights["recency"] * rec[n]
            + weights["omission"] * omi[n]
            + weights["transition"] * tra[n]
            + 0.002
            for n in range(10)
        ]
        stats.append(normalize_scores(probs))
    return stats


def normalize_scores(values: list[float]) -> list[float]:
    low = min(values) if values else 0.0
    shifted = [v - low + 0.0001 for v in values]
    total = sum(shifted)
    if total <= 0:
        return [1.0 / len(values)] * len(values)
    return [v / total for v in shifted]


def safe_prob(value: float) -> float:
    return max(value, 1e-9)


def draw_shape(numbers: tuple[int, ...]) -> tuple[int, int, int, int, int, bool]:
    return (
        sum(numbers),
        max(numbers) - min(numbers),
        sum(1 for n in numbers if n % 2 == 1),
        sum(1 for n in numbers if n >= 5),
        sum(numbers) % 10,
        len(set(numbers)) < len(numbers),
    )


def bayes_position_probs(draws: list[Draw], digits: int, alpha: float = 1.2) -> list[list[float]]:
    recent = draws[-60:] if len(draws) > 60 else draws
    probs: list[list[float]] = []
    for pos in range(digits):
        counts = [alpha] * 10
        for idx, draw in enumerate(recent):
            counts[draw.numbers[pos]] += 1.0 + idx / max(1, len(recent)) * 0.35
        probs.append([c / sum(counts) for c in counts])
    return probs


def markov_position_probs(draws: list[Draw], digits: int, alpha: float = 0.6) -> list[list[float]]:
    if not draws:
        return [[0.1] * 10 for _ in range(digits)]
    probs: list[list[float]] = []
    latest = draws[-1].numbers
    for pos in range(digits):
        one_counts = [alpha] * 10
        two_counts = [alpha] * 10
        prev_digit = latest[pos]
        prev_pair = tuple(d.numbers[pos] for d in draws[-2:]) if len(draws) >= 2 else None
        for before, after in zip(draws[:-1], draws[1:]):
            if before.numbers[pos] == prev_digit:
                one_counts[after.numbers[pos]] += 1.0
        if prev_pair and len(draws) >= 3:
            for idx in range(len(draws) - 2):
                pair = (draws[idx].numbers[pos], draws[idx + 1].numbers[pos])
                if pair == prev_pair:
                    two_counts[draws[idx + 2].numbers[pos]] += 1.4
        one = [c / sum(one_counts) for c in one_counts]
        two = [c / sum(two_counts) for c in two_counts]
        probs.append([0.58 * one[n] + 0.42 * two[n] for n in range(10)])
    return probs


def shape_model(draws: list[Draw]) -> dict[str, Any]:
    recent = draws[-80:] if len(draws) > 80 else draws
    if not recent:
        return {}
    latest_shape = draw_shape(recent[-1].numbers)
    transitions: dict[tuple[Any, ...], int] = {}
    for before, after in zip(recent[:-1], recent[1:]):
        before_key = coarse_shape(draw_shape(before.numbers))
        if before_key == coarse_shape(latest_shape):
            after_key = coarse_shape(draw_shape(after.numbers))
            transitions[after_key] = transitions.get(after_key, 0) + 1
    marginal: dict[tuple[Any, ...], int] = {}
    for draw in recent:
        key = coarse_shape(draw_shape(draw.numbers))
        marginal[key] = marginal.get(key, 0) + 1
    return {"transition": transitions, "marginal": marginal, "latest": coarse_shape(latest_shape)}


def coarse_shape(shape: tuple[int, int, int, int, int, bool]) -> tuple[Any, ...]:
    digit_sum, span, odd, big, tail, repeat = shape
    sum_bucket = digit_sum // 4
    span_bucket = min(span // 2, 4)
    return (sum_bucket, span_bucket, odd, big, tail, repeat)


def shape_probability(numbers: tuple[int, ...], model: dict[str, Any]) -> float:
    if not model:
        return 0.1
    key = coarse_shape(draw_shape(numbers))
    transition = model.get("transition", {})
    marginal = model.get("marginal", {})
    t_total = sum(transition.values())
    m_total = sum(marginal.values())
    t_prob = (transition.get(key, 0) + 0.7) / (t_total + 0.7 * max(1, len(marginal)))
    m_prob = (marginal.get(key, 0) + 0.7) / (m_total + 0.7 * max(1, len(marginal)))
    return 0.62 * t_prob + 0.38 * m_prob


def log_position_prob(numbers: tuple[int, ...], probs: list[list[float]]) -> float:
    return sum(math.log(safe_prob(probs[pos][n])) for pos, n in enumerate(numbers))


def ensemble_components(numbers: tuple[int, ...], stats: list[list[float]], draws: list[Draw], signal: dict[str, Any] | None = None) -> dict[str, float]:
    digits = len(numbers)
    bayes_probs = bayes_position_probs(draws, digits)
    markov_probs = markov_position_probs(draws, digits)
    shape = shape_model(draws)
    legacy = candidate_score(numbers, stats, draws, signal)
    markov = log_position_prob(numbers, markov_probs)
    bayes = log_position_prob(numbers, bayes_probs)
    shape_score = math.log(safe_prob(shape_probability(numbers, shape)))
    return {
        "legacy": legacy,
        "markov": markov,
        "bayes": bayes,
        "shape": shape_score,
    }


def ensemble_score(components: dict[str, float], model_weights: dict[str, float]) -> float:
    total = 0.0
    weight_sum = 0.0
    for key, value in components.items():
        weight = float(model_weights.get(key, 0.0))
        total += weight * value
        weight_sum += weight
    return total / max(weight_sum, 1e-9)


def candidate_score(numbers: tuple[int, ...], stats: list[list[float]], draws: list[Draw], signal: dict[str, Any] | None = None) -> float:
    score = 0.0
    for pos, n in enumerate(numbers):
        score += math.log(stats[pos][n] + 1e-9)

    digit_sum = sum(numbers)
    span = max(numbers) - min(numbers)
    recent = draws[-80:] if len(draws) > 80 else draws
    if recent:
        sums = [sum(d.numbers) for d in recent]
        spans = [max(d.numbers) - min(d.numbers) for d in recent]
        score += gaussian_bonus(digit_sum, mean(sums), stddev(sums))
        score += 0.6 * gaussian_bonus(span, mean(spans), stddev(spans))
        score += trend_shape_bonus(numbers, recent)
    if signal:
        score += signal_bonus(numbers, signal)
    repeat_penalty = len(numbers) - len(set(numbers))
    return score - repeat_penalty * 0.08


def signal_bonus(numbers: tuple[int, ...], signal: dict[str, Any]) -> float:
    weight = float(signal.get("weight", DEFAULT_CONFIG["signal_weight"]))
    bonus = 0.0
    for field, field_weight in [("test_number", 0.55), ("machine_number", 0.36), ("focus_number", 0.24)]:
        digits = signal.get(field)
        if not digits:
            continue
        comparable = tuple(digits[: len(numbers)])
        pos_hits = sum(1 for a, b in zip(numbers, comparable) if a == b)
        overlap = len(set(numbers) & set(comparable))
        bonus += weight * field_weight * (pos_hits * 0.9 + overlap * 0.25)
    return bonus


def align_pls_plw(report: dict[str, Any]) -> None:
    pls = report["lotteries"].get("pls")
    plw = report["lotteries"].get("plw")
    if not pls or not plw:
        return
    pls_heads = [candidate["number"] for candidate in pls.get("top3", pls.get("candidates", [])[:3])]
    if not pls_heads:
        return
    aligned = []
    seen = set()
    for head in pls_heads:
        for candidate in plw.get("candidates", []):
            number = candidate["number"]
            if number.startswith(head) and number not in seen:
                aligned.append(candidate)
                seen.add(number)
                break
    for candidate in plw.get("candidates", []):
        if len(aligned) >= 3:
            break
        if candidate["number"] not in seen:
            aligned.append(candidate)
            seen.add(candidate["number"])
    if aligned:
        plw["top3"] = aligned[:3]


def trend_shape_bonus(numbers: tuple[int, ...], recent: list[Draw]) -> float:
    """Score shape features commonly used in lottery trend charts."""
    odd_count = sum(1 for n in numbers if n % 2 == 1)
    big_count = sum(1 for n in numbers if n >= 5)
    sum_tail = sum(numbers) % 10
    mod3_counts = tuple(sum(1 for n in numbers if n % 3 == m) for m in range(3))

    recent_odd = [sum(1 for n in d.numbers if n % 2 == 1) for d in recent]
    recent_big = [sum(1 for n in d.numbers if n >= 5) for d in recent]
    recent_tail = [sum(d.numbers) % 10 for d in recent]
    recent_mod3 = [tuple(sum(1 for n in d.numbers if n % 3 == m) for m in range(3)) for d in recent]

    bonus = 0.0
    bonus += 0.35 * categorical_bonus(odd_count, recent_odd)
    bonus += 0.35 * categorical_bonus(big_count, recent_big)
    bonus += 0.25 * categorical_bonus(sum_tail, recent_tail)
    bonus += 0.25 * categorical_bonus(mod3_counts, recent_mod3)

    # Mild reversal pressure after very concentrated recent shapes.
    latest = recent[-1].numbers
    latest_delta = sum(abs(a - b) for a, b in zip(numbers, latest))
    avg_delta = mean([sum(abs(a - b) for a, b in zip(d.numbers, latest)) for d in recent[-20:]])
    bonus += 0.15 * gaussian_bonus(latest_delta, avg_delta, max(stddev([sum(abs(a - b) for a, b in zip(d.numbers, latest)) for d in recent[-20:]]), 1.0))
    return bonus


def categorical_bonus(value: Any, samples: list[Any]) -> float:
    if not samples:
        return 0.0
    count = sum(1 for sample in samples if sample == value)
    rate = count / len(samples)
    return math.log(rate + 0.05)


def trend_summary(draws: list[Draw]) -> dict[str, Any]:
    recent = draws[-30:] if len(draws) > 30 else draws
    if not recent:
        return {}
    sums = [sum(d.numbers) for d in recent]
    spans = [max(d.numbers) - min(d.numbers) for d in recent]
    odd_counts = [sum(1 for n in d.numbers if n % 2 == 1) for d in recent]
    big_counts = [sum(1 for n in d.numbers if n >= 5) for d in recent]
    tails = [sum(d.numbers) % 10 for d in recent]
    return {
        "recent_window": len(recent),
        "sum_avg": round(mean(sums), 2),
        "sum_last": sums[-1],
        "span_avg": round(mean(spans), 2),
        "span_last": spans[-1],
        "most_common_odd_count": most_common(odd_counts),
        "most_common_big_count": most_common(big_counts),
        "hot_sum_tails": top_counts(tails, 3),
    }


def most_common(values: list[Any]) -> Any:
    return top_counts(values, 1)[0][0]


def top_counts(values: list[Any], limit: int) -> list[list[Any]]:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return [[key, count] for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def fetch_17500_signals(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        text = fetch_text("https://www.17500.cn/", int(config["request_timeout_seconds"]), config["user_agent"])
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError):
        return {}
    return parse_17500_signals(text, float(config.get("signal_weight", DEFAULT_CONFIG["signal_weight"])))


def parse_17500_signals(text: str, weight: float) -> dict[str, dict[str, Any]]:
    clean = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    clean = " ".join(clean.split())
    game_patterns = {
        "fc3d": ("福彩3D", 3),
        "pls": ("排列3", 3),
        "plw": ("排列5", 5),
    }
    signals: dict[str, dict[str, Any]] = {}
    for key, (label, digits) in game_patterns.items():
        label_pos = clean.find(label)
        if label_pos < 0:
            continue
        block = clean[label_pos : label_pos + 900]
        issue_match = re.search(r"第\s*(\d+)\s*期", block)
        draw_match = re.search(rf"开奖[^0-9]*((?:\d\s*){{{digits}}})", block)
        machine_match = re.search(rf"开机号[:：]?\s*((?:\d\s*){{{digits}}})", block)
        test_match = re.search(rf"试机号[:：]?\s*((?:\d\s*){{{digits}}})", block)
        focus_match = re.search(rf"关注码[:：]?\s*((?:\d\s*){{{digits}}})", block)
        signal = {
            "source": "https://www.17500.cn/",
            "issue_hint": issue_match.group(1) if issue_match else "",
            "weight": weight,
        }
        for field, match in [
            ("draw_number", draw_match),
            ("machine_number", machine_match),
            ("test_number", test_match),
            ("focus_number", focus_match),
        ]:
            if match:
                parsed = parse_digits(match.group(1), digits)
                if parsed:
                    signal[field] = parsed
        if any(field in signal for field in ["machine_number", "test_number", "focus_number"]):
            signals[key] = signal
    return signals


def signal_for_report(signal: dict[str, Any] | None) -> dict[str, Any]:
    if not signal:
        return {}
    result = {k: v for k, v in signal.items() if k not in {"weight"}}
    for key, value in list(result.items()):
        if isinstance(value, tuple):
            result[key] = "".join(str(x) for x in value)
    return result


def gaussian_bonus(value: float, avg: float, sd: float) -> float:
    sd = max(sd, 1.0)
    z = (value - avg) / sd
    return -0.5 * z * z


def mean(values: list[int]) -> float:
    return sum(values) / max(1, len(values))


def stddev(values: list[int]) -> float:
    avg = mean(values)
    return math.sqrt(sum((x - avg) ** 2 for x in values) / max(1, len(values)))


def generate_candidates(
    draws: list[Draw],
    digits: int,
    count: int,
    weights: dict[str, float],
    signal: dict[str, Any] | None = None,
    model_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    stats = position_stats(draws, digits, weights)
    model_weights = model_weights or DEFAULT_CONFIG["model_weights"]
    all_numbers = candidate_pool(stats, digits, draws)
    scored = []
    for numbers in all_numbers:
        components = ensemble_components(numbers, stats, draws, signal)
        scored.append((ensemble_score(components, model_weights), numbers, components))
    scored.sort(reverse=True, key=lambda x: x[0])
    top = scored[: max(count * 5, count)]
    # Add slight diversity so the report is not only near-duplicates.
    chosen: list[tuple[float, tuple[int, ...], dict[str, float]]] = []
    model_leaders = []
    for model_name in ["markov", "bayes", "shape"]:
        leader = max(scored[: max(300, count * 20)], key=lambda item: item[2].get(model_name, -999))
        model_leaders.append(leader)
    for item in [scored[0], *model_leaders]:
        if item not in chosen:
            chosen.append(item)

    for score, numbers, components in top:
        if len(chosen) >= count:
            break
        if all(hamming(numbers, other) >= max(1, digits // 2) for _, other, _ in chosen[:10]):
            chosen.append((score, numbers, components))
    for item in top:
        if len(chosen) >= count:
            break
        if item not in chosen:
            chosen.append(item)
    return [
        {
            "rank": idx + 1,
            "number": "".join(str(x) for x in numbers),
            "score": round(score, 6),
            "sum": sum(numbers),
            "span": max(numbers) - min(numbers),
            "models": {key: round(value, 4) for key, value in components.items()},
        }
        for idx, (score, numbers, components) in enumerate(chosen[:count])
    ]


def candidate_pool(stats: list[list[float]], digits: int, draws: list[Draw] | None = None) -> Iterable[tuple[int, ...]]:
    draws = draws or []
    pools: set[tuple[int, ...]] = set()
    if digits <= 3:
        pools.update(itertools.product(range(10), repeat=digits))
        return pools

    # Pailie 5 has 100,000 full combinations. During daily backtests that is
    # unnecessarily slow, so score the strongest per-position chart candidates
    # plus a small deterministic diversity set.
    model_prob_sets = [stats]
    if draws:
        model_prob_sets.extend([markov_position_probs(draws, digits), bayes_position_probs(draws, digits)])

    for probs in model_prob_sets:
        top_digits = []
        for pos in range(digits):
            ranked = sorted(range(10), key=lambda n: probs[pos][n], reverse=True)
            top_digits.append(ranked[:7])
        pools.update(itertools.product(*top_digits))

    shape = shape_model(draws)
    if shape:
        for base in list(pools)[:2000]:
            for pos in range(digits):
                for n in range(10):
                    candidate = tuple(n if idx == pos else value for idx, value in enumerate(base))
                    if shape_probability(candidate, shape) >= shape_probability(base, shape):
                        pools.add(candidate)

    for shift in range(10):
        pools.add(tuple((pos + shift) % 10 for pos in range(digits)))
        pools.add(tuple((9 - pos - shift) % 10 for pos in range(digits)))
    return pools


def hamming(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


def evaluate_prediction(candidates: list[dict[str, Any]], actual: tuple[int, ...]) -> dict[str, Any]:
    actual_text = "".join(str(x) for x in actual)
    best = 0
    exact_rank = None
    for candidate in candidates:
        text = candidate["number"]
        hits = sum(1 for a, b in zip(text, actual_text) if a == b)
        best = max(best, hits)
        if text == actual_text:
            exact_rank = candidate["rank"]
    return {
        "actual": actual_text,
        "best_position_hits": best,
        "exact_rank": exact_rank,
    }


def optimize_weights(draws: list[Draw], digits: int, config: dict[str, Any]) -> dict[str, float]:
    if config.get("fast") or config.get("offline"):
        return config["weights"]
    if len(draws) < 30:
        return config["weights"]
    grids = [
        {"frequency": 0.42, "recency": 0.24, "omission": 0.20, "transition": 0.14},
        {"frequency": 0.34, "recency": 0.28, "omission": 0.22, "transition": 0.16},
        {"frequency": 0.28, "recency": 0.36, "omission": 0.22, "transition": 0.14},
        {"frequency": 0.30, "recency": 0.24, "omission": 0.32, "transition": 0.14},
        {"frequency": 0.30, "recency": 0.24, "omission": 0.18, "transition": 0.28},
    ]
    max_window = 24 if digits >= 5 else int(config["backtest_window"])
    window = min(max_window, len(draws) - 10)
    test_draws = draws[-window:]
    best_score = -1.0
    best_weights = config["weights"]
    for weights in grids:
        score = 0.0
        for idx, actual in enumerate(test_draws):
            cutoff = len(draws) - window + idx
            history = draws[:cutoff]
            candidates = generate_candidates(history, digits, 10, weights, model_weights=config.get("model_weights"))
            result = evaluate_prediction(candidates, actual.numbers)
            score += result["best_position_hits"] / digits
            if result["exact_rank"]:
                score += 1.5
        if score > best_score:
            best_score = score
            best_weights = weights
    return best_weights


def predict(config: dict[str, Any]) -> dict[str, Any]:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    report: dict[str, Any] = {"date": today, "created_at": dt.datetime.now().isoformat(timespec="seconds"), "lotteries": {}}
    signals = fetch_17500_signals(config)
    for key in config["lotteries"]:
        draws, source = collect_lottery(key, config)
        spec = LOTTERIES[key]
        weights = optimize_weights(draws, spec["digits"], config)
        signal = signals.get(key)
        candidates = generate_candidates(draws, spec["digits"], int(config["candidate_count"]), weights, signal, config.get("model_weights"))
        latest = draws[-1] if draws else None
        report["lotteries"][key] = {
            "name": spec["name"],
            "source": source,
            "history_count": len(draws),
            "latest_issue": latest.issue if latest else None,
            "latest_date": latest.date if latest else None,
            "latest_number": "".join(str(x) for x in latest.numbers) if latest else None,
            "weights": weights,
            "model_weights": config.get("model_weights", DEFAULT_CONFIG["model_weights"]),
            "trend_summary": trend_summary(draws),
            "pre_draw_signals": signal_for_report(signal),
            "candidates": candidates,
            "top3": candidates[:3],
            "note": "随机开奖不可预测，本结果仅用于统计记录和复盘。",
        }
    align_pls_plw(report)
    save_json(REPORT_DIR / f"prediction-{today}.json", report)
    write_markdown_report(report, REPORT_DIR / f"prediction-{today}.md")
    write_mobile_report(report, REPORT_DIR / "mobile.html")
    write_status("prediction", report["created_at"])
    return report


def quick_predict(config: dict[str, Any]) -> dict[str, Any]:
    quick_config = dict(config)
    quick_config["fast"] = True
    quick_config["request_timeout_seconds"] = min(5, int(config.get("request_timeout_seconds", 8)))
    quick_config["candidate_count"] = min(10, int(config.get("candidate_count", 20)))
    return predict(quick_config)


def post_draw(config: dict[str, Any]) -> dict[str, Any]:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    prediction_path = REPORT_DIR / f"prediction-{today}.json"
    previous = json.loads(prediction_path.read_text(encoding="utf-8")) if prediction_path.exists() else predict(config)
    review: dict[str, Any] = {"date": today, "created_at": dt.datetime.now().isoformat(timespec="seconds"), "results": {}}
    updated_config = dict(config)
    for key in config["lotteries"]:
        draws, source = collect_lottery(key, config)
        spec = LOTTERIES[key]
        latest = draws[-1]
        candidates = previous["lotteries"].get(key, {}).get("candidates", [])
        evaluation = evaluate_prediction(candidates, latest.numbers) if candidates else {}
        optimized = optimize_weights(draws, spec["digits"], config)
        review["results"][key] = {
            "name": spec["name"],
            "source": source,
            "latest_issue": latest.issue,
            "latest_date": latest.date,
            "latest_number": "".join(str(x) for x in latest.numbers),
            "evaluation": evaluation,
            "next_weights": optimized,
        }
        updated_config["weights"] = optimized
    save_json(REPORT_DIR / f"post-draw-{today}.json", review)
    write_markdown_review(review, REPORT_DIR / f"post-draw-{today}.md")
    save_json(CONFIG_PATH, updated_config)
    write_status("post-draw", review["created_at"])
    return review


def write_status(action: str, created_at: str) -> None:
    save_json(
        REPORT_DIR / "status.json",
        {
            "last_action": action,
            "last_updated": created_at,
            "predict_time": load_config().get("predict_time"),
            "post_draw_time": load_config().get("post_draw_time"),
        },
    )


def publish_site() -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    mobile = REPORT_DIR / "mobile.html"
    if not mobile.exists():
        reports = sorted(REPORT_DIR.glob("prediction-*.json"))
        if reports:
            report = json.loads(reports[-1].read_text(encoding="utf-8"))
            write_mobile_report(report, mobile)
        else:
            write_mobile_placeholder(mobile)
    (DOCS_DIR / "index.html").write_text(mobile.read_text(encoding="utf-8"), encoding="utf-8")
    for name in ["status.json"]:
        source = REPORT_DIR / name
        if source.exists():
            (DOCS_DIR / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    latest_prediction = sorted(REPORT_DIR.glob("prediction-*.json"))
    latest_review = sorted(REPORT_DIR.glob("post-draw-*.json"))
    if latest_prediction:
        (DOCS_DIR / "latest-prediction.json").write_text(latest_prediction[-1].read_text(encoding="utf-8"), encoding="utf-8")
    if latest_review:
        (DOCS_DIR / "latest-review.json").write_text(latest_review[-1].read_text(encoding="utf-8"), encoding="utf-8")


def cloud_update(config: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now()
    did_review = False
    review_at = scheduled_datetime(now.date(), config["post_draw_time"])
    if now >= review_at:
        try:
            post_draw(config)
            did_review = True
        except Exception as exc:
            write_error_status("cloud-post-draw-error", exc)
    report = predict(load_config())
    publish_site()
    save_json(
        REPORT_DIR / "cloud-run.json",
        {
            "ran_at": now.isoformat(timespec="seconds"),
            "did_review": did_review,
            "published": str(DOCS_DIR / "index.html"),
        },
    )
    return report


def write_error_status(action: str, exc: Exception) -> None:
    save_json(
        REPORT_DIR / "status.json",
        {
            "last_action": action,
            "last_updated": dt.datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
            "predict_time": load_config().get("predict_time"),
            "post_draw_time": load_config().get("post_draw_time"),
        },
    )


def mobile_status_summary() -> str:
    path = REPORT_DIR / "status.json"
    if not path.exists():
        return ""
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return html.escape(json.dumps(status, ensure_ascii=False))


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    lines = [f"# 彩票统计预测报告 {report['date']}", "", "> 随机开奖不可预测，本报告只做统计复盘和候选组合记录。", ""]
    for item in report["lotteries"].values():
        top3_text = "、".join(c["number"] for c in item.get("top3", item["candidates"][:3]))
        lines.extend(
            [
                f"## {item['name']}",
                f"- 最高评分 3 码：{top3_text}",
                f"- 数据来源：{item['source']}",
                f"- 开机/试机/关注码：{json.dumps(item.get('pre_draw_signals', {}), ensure_ascii=False)}",
                f"- 历史期数：{item['history_count']}",
                f"- 最新开奖：{item['latest_issue']} / {item['latest_date']} / {item['latest_number']}",
                f"- 权重：{json.dumps(item['weights'], ensure_ascii=False)}",
                f"- 模型权重：{json.dumps(item.get('model_weights', {}), ensure_ascii=False)}",
                f"- 近期开奖形态：{json.dumps(item['trend_summary'], ensure_ascii=False)}",
                "",
                "| 排名 | 号码 | 分数 | 和值 | 跨度 |",
                "|---:|---:|---:|---:|---:|",
            ]
        )
        for c in item["candidates"]:
            lines.append(f"| {c['rank']} | {c['number']} | {c['score']} | {c['sum']} | {c['span']} |")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_mobile_report(report: dict[str, Any], path: Path) -> None:
    cards = []
    for item in report["lotteries"].values():
        top3 = item.get("top3") or item.get("candidates", [])[:3]
        pills = "\n".join(
            f'<div class="pick"><span>{html.escape(str(c["number"]))}</span><small>#{c["rank"]} score {c["score"]}</small></div>'
            for c in top3
        )
        trend = html.escape(json.dumps(item.get("trend_summary", {}), ensure_ascii=False))
        pre_draw = html.escape(json.dumps(item.get("pre_draw_signals", {}), ensure_ascii=False))
        models = html.escape(json.dumps({c["number"]: c.get("models", {}) for c in top3}, ensure_ascii=False))
        cards.append(
            f"""
            <section class="card">
              <div class="meta">{html.escape(str(item.get("latest_issue", "")))} / {html.escape(str(item.get("latest_date", "")))}</div>
              <h2>{html.escape(str(item["name"]))}</h2>
              <div class="picks">{pills}</div>
              <div class="latest">data source: {html.escape(str(item.get("source", "")))}</div>
              <div class="latest">latest draw: {html.escape(str(item.get("latest_number", "")))}</div>
              <details><summary>model details</summary><pre>{models}</pre></details>
              <details><summary>machine/test/focus</summary><pre>{pre_draw}</pre></details>
              <details><summary>trend summary</summary><pre>{trend}</pre></details>
            </section>
            """
        )
    status_payload = {
        "served_at": dt.datetime.now().isoformat(timespec="seconds"),
        "report_created_at": report.get("created_at"),
        "today": dt.date.today().isoformat(),
        "status": json.loads((REPORT_DIR / "status.json").read_text(encoding="utf-8")) if (REPORT_DIR / "status.json").exists() else {},
    }
    status_summary = html.escape(json.dumps(status_payload, ensure_ascii=False))
    status_block = f"<details><summary>system status</summary><pre>{status_summary}</pre></details>" if status_summary else ""
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="refresh" content="300">
  <title>Lottery Top 3 - {html.escape(str(report["date"]))}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f5f7fa; color: #172033; }}
    header {{ padding: 22px 16px 10px; }}
    h1 {{ margin: 0; font-size: 24px; line-height: 1.2; }}
    .sub {{ margin-top: 8px; color: #627086; font-size: 13px; }}
    main {{ padding: 8px 12px 28px; display: grid; gap: 12px; }}
    .card {{ background: white; border: 1px solid #dde3ec; border-radius: 8px; padding: 14px; box-shadow: 0 8px 24px rgba(22, 34, 51, .06); }}
    .meta {{ color: #7a8699; font-size: 12px; }}
    h2 {{ margin: 6px 0 12px; font-size: 18px; }}
    .picks {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
    .pick {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px 6px; text-align: center; background: #f8fafc; min-width: 0; }}
    .pick span {{ display: block; font-size: 25px; font-weight: 800; letter-spacing: 0; color: #b42318; overflow-wrap: anywhere; }}
    .pick small {{ display: block; margin-top: 4px; font-size: 10px; color: #64748b; }}
    .latest {{ margin-top: 12px; color: #475569; font-size: 13px; }}
    details {{ margin-top: 10px; font-size: 12px; color: #475569; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    footer {{ padding: 0 16px 22px; color: #7a8699; font-size: 12px; line-height: 1.5; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #0f172a; color: #e5e7eb; }}
      .card {{ background: #111827; border-color: #263244; box-shadow: none; }}
      .pick {{ background: #172033; border-color: #334155; }}
      .pick span {{ color: #fca5a5; }}
      .sub, .meta, .latest, details, footer, .pick small {{ color: #94a3b8; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Daily Top 3</h1>
    <div class="sub">{html.escape(str(report["date"]))} generated at {html.escape(str(report["created_at"]))}</div>
  </header>
  <main>
    {''.join(cards)}
  </main>
  <footer>
    Lottery draws are random. This page shows the three highest-scored statistical candidates for each game and refreshes every 5 minutes.
    {status_block}
  </footer>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def write_markdown_review(review: dict[str, Any], path: Path) -> None:
    lines = [f"# 开奖后复盘 {review['date']}", "", "> 复盘用于调整下一次统计权重，不表示存在稳定预测能力。", ""]
    for item in review["results"].values():
        lines.extend(
            [
                f"## {item['name']}",
                f"- 数据来源：{item['source']}",
                f"- 开奖：{item['latest_issue']} / {item['latest_date']} / {item['latest_number']}",
                f"- 命中复盘：{json.dumps(item['evaluation'], ensure_ascii=False)}",
                f"- 下一轮权重：{json.dumps(item['next_weights'], ensure_ascii=False)}",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def sleep_until(target_hhmm: str) -> None:
    hour, minute = [int(x) for x in target_hhmm.split(":", 1)]
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    time.sleep((target - now).total_seconds())


def parse_hhmm(value: str) -> dt.time:
    hour, minute = [int(x) for x in value.split(":", 1)]
    return dt.time(hour=hour, minute=minute)


def scheduled_datetime(day: dt.date, hhmm: str) -> dt.datetime:
    parsed = parse_hhmm(hhmm)
    return dt.datetime.combine(day, parsed)


def report_is_fresh(prefix: str, day: dt.date, scheduled_at: dt.datetime) -> bool:
    path = REPORT_DIR / f"{prefix}-{day.strftime('%Y-%m-%d')}.json"
    if not path.exists():
        return False
    modified = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return modified >= scheduled_at


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def should_refresh_prediction(config: dict[str, Any], force: bool) -> bool:
    if force:
        return True
    today = dt.date.today()
    path = REPORT_DIR / f"prediction-{today.strftime('%Y-%m-%d')}.json"
    if not path.exists():
        return True
    minutes = int(config.get("web_refresh_predict_minutes", DEFAULT_CONFIG["web_refresh_predict_minutes"]))
    if minutes <= 0:
        return True
    modified = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return (dt.datetime.now() - modified) >= dt.timedelta(minutes=minutes)


def should_refresh_review(config: dict[str, Any], force: bool) -> bool:
    if config.get("web_refresh_post_draw_always"):
        return True
    now = dt.datetime.now()
    today = now.date()
    if now < scheduled_datetime(today, config["post_draw_time"]):
        return False
    path = REPORT_DIR / f"post-draw-{today.strftime('%Y-%m-%d')}.json"
    if force:
        return True
    if not path.exists():
        return True
    return dt.datetime.fromtimestamp(path.stat().st_mtime) < scheduled_datetime(today, config["post_draw_time"])


def refresh_from_web_request(force: bool = False) -> None:
    global LAST_WEB_REFRESH_AT
    if not WEB_REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        config = load_config()
        if should_refresh_review(config, force):
            try:
                post_draw(load_config())
            except Exception as exc:
                write_error_status("web-post-draw-error", exc)
        if should_refresh_prediction(load_config(), force):
            try:
                predict(load_config())
            except Exception as exc:
                write_error_status("web-predict-error", exc)
                raise
        LAST_WEB_REFRESH_AT = dt.datetime.now()
    finally:
        WEB_REFRESH_LOCK.release()


class RefreshingMobileHandler(NoCacheHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/mobile.html"}:
            query = urllib.parse.parse_qs(parsed.query)
            force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
            try:
                refresh_from_web_request(force=force)
            except Exception:
                # Serve the last good page if live refresh fails.
                pass
            if parsed.path == "/":
                self.path = "/mobile.html"
        return super().do_GET()


def run_daemon(config: dict[str, Any]) -> None:
    print("Lottery predictor daemon started. Press Ctrl+C to stop.", flush=True)
    while True:
        now_text = dt.datetime.now().strftime("%H:%M")
        if now_text == config["predict_time"]:
            predict(config)
            time.sleep(65)
        elif now_text == config["post_draw_time"]:
            post_draw(load_config())
            time.sleep(65)
        else:
            time.sleep(20)


def get_lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def serve_mobile(port: int = 8765) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    mobile_path = REPORT_DIR / "mobile.html"
    if not mobile_path.exists():
        today_reports = sorted(REPORT_DIR.glob("prediction-*.json"))
        if today_reports:
            report = json.loads(today_reports[-1].read_text(encoding="utf-8"))
            write_mobile_report(report, mobile_path)
        else:
            write_mobile_placeholder(mobile_path)
    os.chdir(REPORT_DIR)
    lan_ip = get_lan_ip()
    print("Mobile page is ready.", flush=True)
    print(f"Phone on the same Wi-Fi: http://{lan_ip}:{port}/mobile.html", flush=True)
    print(f"This computer: http://127.0.0.1:{port}/mobile.html", flush=True)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), RefreshingMobileHandler)
    server.serve_forever()


def run_autopilot(config: dict[str, Any], port: int = 8765) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    os.chdir(REPORT_DIR)
    lan_ip = get_lan_ip()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), RefreshingMobileHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Mobile page: http://{lan_ip}:{port}/mobile.html", flush=True)
    print(f"Autopilot started: predict at {config['predict_time']}, post-draw review at {config['post_draw_time']}.", flush=True)
    last_predict_date = ""
    last_review_date = ""
    while True:
        now = dt.datetime.now()
        today_date = now.date()
        today = today_date.strftime("%Y-%m-%d")
        try:
            current_config = load_config()
            predict_at = scheduled_datetime(today_date, current_config["predict_time"])
            review_at = scheduled_datetime(today_date, current_config["post_draw_time"])
            if now >= predict_at and last_predict_date != today and not report_is_fresh("prediction", today_date, predict_at):
                predict(current_config)
                last_predict_date = today
                print(f"Prediction generated for {today}", flush=True)
            if now >= review_at and last_review_date != today and not report_is_fresh("post-draw", today_date, review_at):
                post_draw(current_config)
                last_review_date = today
                print(f"Post-draw review completed for {today}", flush=True)
        except Exception as exc:
            print(f"Autopilot error: {exc}", flush=True)
        time.sleep(20)


def purge_bad_cache() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    for key, spec in LOTTERIES.items():
        path = data_file(key)
        if not path.exists():
            continue
        draws: list[Draw] = []
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                numbers = parse_digits(row.get("numbers", ""), spec["digits"])
                if numbers:
                    draws.append(Draw(row["issue"], row.get("date", ""), numbers))
        if not is_valid_history(draws, spec["digits"]):
            backup = path.with_suffix(f".bad-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.csv")
            path.replace(backup)
            print(f"Moved invalid cache: {path.name} -> {backup.name}")
    for path in [REPORT_DIR / "mobile.html", REPORT_DIR / "status.json", *REPORT_DIR.glob("prediction-*.json"), *REPORT_DIR.glob("prediction-*.md")]:
        if path.exists():
            backup = path.with_suffix(f".old-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}{path.suffix}")
            path.replace(backup)
            print(f"Moved old report: {path.name} -> {backup.name}")


def rebuild_real_prediction(config: dict[str, Any]) -> dict[str, Any]:
    purge_bad_cache()
    real_config = dict(config)
    real_config["fast"] = True
    real_config["candidate_count"] = min(10, int(config.get("candidate_count", 20)))
    return predict(real_config)


def write_mobile_placeholder(path: Path) -> None:
    document = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Lottery Predictor</title>
  <style>
    body { margin: 0; padding: 28px 18px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f7fa; color: #172033; }
    .box { max-width: 520px; margin: 0 auto; background: white; border: 1px solid #dde3ec; border-radius: 8px; padding: 18px; }
    h1 { margin: 0 0 12px; font-size: 22px; }
    p { line-height: 1.6; color: #475569; }
    code { background: #eef2f7; padding: 2px 5px; border-radius: 4px; }
  </style>
</head>
<body>
  <div class="box">
    <h1>预测页面还没有生成</h1>
    <p>请先在电脑 PowerShell 里运行：<br><code>python .\\lottery_predictor.py predict</code></p>
    <p>生成后刷新本页面，就会看到 3D、排列三、排列五各 3 个最高评分号码。</p>
  </div>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect lottery history and generate statistical predictions.")
    parser.add_argument("command", choices=["predict", "quick", "rebuild", "post-draw", "collect", "daemon", "serve", "autopilot", "cloud", "publish-site", "purge-cache", "init"], help="Action to run")
    parser.add_argument("--port", type=int, default=8765, help="Port for the mobile report server")
    args = parser.parse_args(argv)
    random.seed(dt.date.today().isoformat())
    config = load_config()
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)

    if args.command == "init":
        print(f"Config: {CONFIG_PATH}")
        print(f"Data: {DATA_DIR}")
        print(f"Reports: {REPORT_DIR}")
    elif args.command == "collect":
        for key in config["lotteries"]:
            draws, source = collect_lottery(key, config)
            print(f"{LOTTERIES[key]['name']}: {len(draws)} draws from {source}")
    elif args.command == "purge-cache":
        purge_bad_cache()
    elif args.command == "predict":
        report = predict(config)
        print(f"Prediction written for {report['date']}: {REPORT_DIR}")
    elif args.command == "quick":
        report = quick_predict(config)
        print(f"Quick prediction written for {report['date']}: {REPORT_DIR}")
    elif args.command == "rebuild":
        report = rebuild_real_prediction(config)
        print(f"Rebuilt real prediction for {report['date']}: {REPORT_DIR}")
    elif args.command == "post-draw":
        review = post_draw(config)
        print(f"Post-draw review written for {review['date']}: {REPORT_DIR}")
    elif args.command == "cloud":
        report = cloud_update(config)
        print(f"Cloud site updated for {report['date']}: {DOCS_DIR / 'index.html'}")
    elif args.command == "publish-site":
        publish_site()
        print(f"Site published locally: {DOCS_DIR / 'index.html'}")
    elif args.command == "daemon":
        run_daemon(config)
    elif args.command == "serve":
        serve_mobile(args.port)
    elif args.command == "autopilot":
        run_autopilot(config, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
