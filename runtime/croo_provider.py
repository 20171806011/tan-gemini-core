#!/usr/bin/env python3
"""TAN Gemini Core — Autonomous CROO Network Multi-Agent Provider & Analyzer.

Connects to CROO Network (Base Mainnet CAP escrow, USDC settlement) via
WebSocket + periodic HTTP polling fallback, fulfills incoming paid orders
using real deterministic analyzers and live public RPC/HTTP probes, and logs
receipts to memory/croo_orders.jsonl.

Secrets are loaded strictly from /home/antigravity/.tan-secrets/croo_agents.json
(chmod 0600) and are never printed or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [croo-provider] %(levelname)s %(message)s",
)
logging.getLogger("croo").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("tan.croo")

SECRETS_PATH = Path("/home/antigravity/.tan-secrets/croo_agents.json")
CORE_ROOT = Path("/home/antigravity/20171806011/tan-gemini-core")
ORDER_LOG_PATH = CORE_ROOT / "memory" / "croo_orders.jsonl"

CROO_API_URL = "https://api.croo.network"
CROO_WS_URL = "wss://api.croo.network/ws"

CHAIN_RPCS: dict[str, tuple[str, str]] = {
    "base": ("https://mainnet.base.org", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    "ethereum": ("https://ethereum-rpc.publicnode.com", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    "optimism": ("https://mainnet.optimism.io", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
    "arbitrum": ("https://arb1.arbitrum.io/rpc", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
}

EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
ZOS_IMPL_SLOT = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
TRUSTED_PROXY_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_safe_public_url(url: str) -> tuple[bool, str]:
    """Reject non-http(s) and private/loopback/link-local SSRF targets."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported scheme: {parsed.scheme}"
    host = parsed.hostname
    if not host:
        return False, "missing hostname"
    if host.lower() in TRUSTED_PROXY_HOSTS and parsed.scheme == "https":
        return True, "ok"
    if host.lower() in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False, "loopback/local host blocked"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception as exc:
        return False, f"DNS resolution failed: {exc}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return False, f"non-public IP blocked ({ip_str})"
        except ValueError:
            return False, f"invalid resolved IP ({ip_str})"
    return True, "ok"


def _http_fetch(url: str, timeout: int = 8, max_bytes: int = 120_000) -> dict[str, Any]:
    ok, reason = _is_safe_public_url(url)
    if not ok:
        return {"ok": False, "status": 0, "error": reason, "headers": {}, "body": "", "latency_ms": 0}
    t0 = time.monotonic()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TAN-ChainCheck/1.0 (+https://github.com/20171806011/tan-gemini-core)",
            "Accept": "application/json,text/markdown,text/plain,text/html,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes)
            ms = int((time.monotonic() - t0) * 1000)
            return {
                "ok": True,
                "status": resp.status,
                "final_url": resp.geturl(),
                "headers": {k.lower(): v for k, v in resp.headers.items()},
                "body": raw.decode("utf-8", "replace"),
                "bytes": len(raw),
                "latency_ms": ms,
            }
    except urllib.error.HTTPError as e:
        ms = int((time.monotonic() - t0) * 1000)
        try:
            body = e.read(max_bytes).decode("utf-8", "replace")
        except Exception:
            body = ""
        return {
            "ok": False,
            "status": e.code,
            "final_url": url,
            "headers": {k.lower(): v for k, v in (e.headers.items() if e.headers else [])},
            "body": body,
            "bytes": len(body.encode("utf-8")),
            "latency_ms": ms,
            "error": f"HTTP {e.code}",
        }
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "status": 0, "error": str(exc), "headers": {}, "body": "", "latency_ms": ms}


def _estimate_tokens(text: str) -> int:
    """Fast deterministic BPE-like token estimate (~3.7 chars/tok + punct penalty)."""
    if not text:
        return 0
    words = re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", text)
    char_est = math.ceil(len(text) / 3.8)
    word_est = int(math.ceil(len(words) * 1.12))
    return max(1, (char_est + word_est) // 2)


def _extract_first_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s<>\"')]+", text)
    return m.group(0).rstrip(".,;:") if m else None


def _extract_github_repo(text: str) -> tuple[str, str] | None:
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text)
    if m:
        return m.group(1), m.group(2).removesuffix(".git")
    m2 = re.match(r"^\s*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\s*$", text.strip())
    if m2:
        return m2.group(1), m2.group(2).removesuffix(".git")
    return None


# ============================================================================
# 1. TAN TokenDiet Optimizer Handlers
# ============================================================================

FILLER_PATTERNS = [
    (re.compile(r"\b(?:please\s+note\s+that|it\s+is\s+important\s+to\s+note\s+that|keep\s+in\s+mind\s+that)\b", re.I), "Note:"),
    (re.compile(r"\b(?:in\s+order\s+to)\b", re.I), "to"),
    (re.compile(r"\b(?:at\s+this\s+point\s+in\s+time|at\s+the\s+present\s+time)\b", re.I), "now"),
    (re.compile(r"\b(?:due\s+to\s+the\s+fact\s+that|owing\s+to\s+the\s+fact\s+that)\b", re.I), "because"),
    (re.compile(r"\b(?:for\s+the\s+purpose\s+of)\b", re.I), "for"),
    (re.compile(r"\b(?:a\s+large\s+number\s+of|a\s+wide\s+variety\s+of)\b", re.I), "many"),
    (re.compile(r"\b(?:make\s+sure\s+that\s+you\s+always|be\s+sure\s+to\s+always|you\s+should\s+make\s+sure\s+to)\b", re.I), "Always"),
    (re.compile(r"\b(?:as\s+an\s+AI\s+(?:language\s+)?model,?\s*)", re.I), ""),
    (re.compile(r"\b(?:utilize|utilization)\b", re.I), "use"),
]


def run_tokendiet_prompt_audit(req_text: str) -> str:
    source_note = "inline input"
    content = (req_text or "").strip()
    if len(content) < 250:
        url = _extract_first_url(content)
        if url and not content.startswith("{"):
            fetched = _http_fetch(url)
            if fetched["ok"] and fetched["body"]:
                content = fetched["body"]
                source_note = f"fetched URL {url} ({fetched['bytes']} bytes)"

    if not content:
        return json.dumps({
            "service": "TAN TokenDiet — Prompt & Context Token Audit",
            "status": "error",
            "error": "No prompt, markdown, JSON schema, or public URL was provided in requirements.",
        }, indent=2)

    orig_chars = len(content)
    orig_lines = content.splitlines()
    orig_toks = _estimate_tokens(content)

    # Detect JSON vs prose/markdown
    is_json = False
    compressed = content
    hotspots: list[str] = []

    try:
        parsed = json.loads(content)
        is_json = True
        # Compact JSON + strip empty/null optional descriptions where safe
        pretty_len = len(content)
        compact = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        ws_saved = pretty_len - len(compact)
        if ws_saved > 0:
            hotspots.append(f"JSON whitespace/indentation overhead: {ws_saved} chars (~{_estimate_tokens(content) - _estimate_tokens(compact)} tokens).")
        if isinstance(parsed, dict) and "tools" in parsed and isinstance(parsed["tools"], list):
            tool_costs = []
            for t in parsed["tools"]:
                if isinstance(t, dict):
                    name = t.get("name", "unnamed")
                    t_tok = _estimate_tokens(json.dumps(t, separators=(",", ":")))
                    tool_costs.append((t_tok, name))
            tool_costs.sort(reverse=True)
            top3 = ", ".join(f"`{n}` (~{c} tok)" for c, n in tool_costs[:5])
            hotspots.append(f"Heaviest MCP/tool definitions ({len(tool_costs)} total): {top3}.")
        compressed = compact
    except Exception:
        # Text / Markdown / System prompt deterministic lossless-style compression
        seen_norm: set[str] = set()
        dup_lines = 0
        blank_runs = 0
        out_lines: list[str] = []
        in_code = False
        prev_blank = False
        for ln in orig_lines:
            stripped = ln.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                out_lines.append(ln.rstrip())
                prev_blank = False
                continue
            if in_code:
                out_lines.append(ln.rstrip())
                prev_blank = False
                continue
            if not stripped:
                if prev_blank:
                    blank_runs += 1
                    continue
                prev_blank = True
                out_lines.append("")
                continue
            prev_blank = False
            # Deduplicate identical long instruction lines (>= 45 chars)
            norm = re.sub(r"\s+", " ", stripped.lower())
            if len(norm) >= 45 and norm in seen_norm:
                dup_lines += 1
                continue
            seen_norm.add(norm)
            c_ln = re.sub(r"[ \t]{2,}", " ", ln.rstrip())
            for pat, repl in FILLER_PATTERNS:
                c_ln = pat.sub(repl, c_ln)
            c_ln = re.sub(r"\s{2,}", " ", c_ln).strip()
            out_lines.append(c_ln)
        compressed = "\n".join(out_lines).strip()
        if dup_lines:
            hotspots.append(f"Removed {dup_lines} duplicate instruction line(s) (>=45 chars).")
        if blank_runs:
            hotspots.append(f"Collapsed {blank_runs} redundant consecutive blank line(s).")
        if len(re.findall(r"```", content)) >= 4:
            hotspots.append("Multiple fenced code blocks detected — move reference examples to lazy-loaded tool/file resources instead of always-on system prompt.")
        long_paras = [i + 1 for i, l in enumerate(orig_lines) if len(l) > 400]
        if long_paras:
            hotspots.append(f"{len(long_paras)} wall-of-text line(s) >400 chars (lines {long_paras[:6]}) — convert to terse bullet rules.")

    comp_chars = len(compressed)
    comp_toks = _estimate_tokens(compressed)
    saved_toks = max(0, orig_toks - comp_toks)
    saved_pct = round((saved_toks / orig_toks * 100.0), 1) if orig_toks else 0.0
    # Monthly cost impact at 10,000 turns/mo @ $3 / 1M input tokens
    monthly_savings_usd = round((saved_toks * 10_000 / 1_000_000) * 3.0, 2)

    if not hotspots:
        hotspots.append("Input is already tightly written; further savings require lazy-loading optional sections or shortening examples.")

    report = [
        "# TAN TokenDiet — Prompt & Context Audit Report",
        f"- **Timestamp (UTC):** {utc_now()}",
        f"- **Source:** {source_note} ({'JSON' if is_json else 'Text/Markdown'})",
        f"- **Input SHA-256:** `{hashlib.sha256(content.encode()).hexdigest()[:16]}`",
        "",
        "## Metrics",
        "| Metric | Original | Compressed | Savings |",
        "|---|---:|---:|---:|",
        f"| Characters | {orig_chars:,} | {comp_chars:,} | {max(0, orig_chars - comp_chars):,} |",
        f"| Est. Tokens | {orig_toks:,} | {comp_toks:,} | {saved_toks:,} ({saved_pct}%) |",
        f"| Lines | {len(orig_lines):,} | {len(compressed.splitlines()):,} | {max(0, len(orig_lines) - len(compressed.splitlines())):,} |",
        f"| Est. Savings @ 10k calls/mo ($3/1M) | — | — | **${monthly_savings_usd:.2f}/mo** |",
        "",
        "## Detected Bloat Hotspots",
    ]
    for h in hotspots:
        report.append(f"- {h}")
    report.extend([
        "",
        "## Compressed Output",
        "```" + ("json" if is_json else "text"),
        compressed[:12000],
        "```",
    ])
    return "\n".join(report)


def run_tokendiet_repo_audit(req_text: str) -> str:
    content = (req_text or "").strip()
    gh_repo = _extract_github_repo(content)
    if not gh_repo:
        # Fallback: treat pasted JSON/schema/doc as context audit
        return run_tokendiet_prompt_audit(content)

    owner, repo = gh_repo
    meta = _http_fetch(f"https://api.github.com/repos/{owner}/{repo}")
    if not meta["ok"]:
        return f"# TAN TokenDiet Repo Audit — Error\nCould not fetch public GitHub repo `{owner}/{repo}`: {meta.get('error')} (HTTP {meta.get('status')}). Ensure the repository is public."
    repo_info = json.loads(meta["body"])
    branch = repo_info.get("default_branch", "main")
    tree_res = _http_fetch(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1", max_bytes=300_000)
    if not tree_res["ok"]:
        return f"# TAN TokenDiet Repo Audit — Error\nCould not read git tree for `{owner}/{repo}@{branch}`: {tree_res.get('error')}."

    tree = json.loads(tree_res["body"]).get("tree", [])
    ctx_files = []
    total_bytes = 0
    for item in tree:
        if item.get("type") != "blob":
            continue
        p = item.get("path", "")
        sz = int(item.get("size") or 0)
        pl = p.lower()
        if any(
            pl.endswith(name) or name in pl
            for name in (
                "agents.md", "claude.md", "cursorrules", "skill.md", "llms.txt",
                "readme.md", "system_prompt", "prompt", "mcp.json", "tools.json", "openapi.json",
            )
        ):
            tok = max(1, sz // 4)
            ctx_files.append((tok, sz, p))
            total_bytes += sz

    ctx_files.sort(reverse=True)
    total_toks = sum(x[0] for x in ctx_files)
    monthly_10k = round((total_toks * 10_000 / 1_000_000) * 3.0, 2)

    lines = [
        f"# TAN TokenDiet — Repository Context Footprint Audit: `{owner}/{repo}`",
        f"- **Timestamp (UTC):** {utc_now()}",
        f"- **Default Branch:** `{branch}` | **Total Indexed Files:** {len(tree):,}",
        f"- **Context/Agent/Doc Files Found:** {len(ctx_files)} ({total_bytes:,} bytes, ~**{total_toks:,} tokens**)",
        f"- **Baseline Cost (if all loaded @ 10k turns/mo, $3/1M):** **${monthly_10k:.2f}/mo**",
        "",
        "## Heaviest Context & Schema Files",
        "| File | Bytes | Est. Tokens | Recommendation |",
        "|---|---:|---:|---|",
    ]
    for tok, sz, p in ctx_files[:20]:
        rec = "OK"
        if tok > 4000:
            rec = "HIGH BLOAT — split into index + lazy-loaded references"
        elif tok > 1500:
            rec = "MEDIUM — tighten prose & remove duplicated examples"
        elif tok > 500:
            rec = "WATCH — prune boilerplate headings"
        lines.append(f"| `{p}` | {sz:,} | ~{tok:,} | {rec} |")
    if not ctx_files:
        lines.append("| *(none found)* | 0 | 0 | Add concise `AGENTS.md` (<500 tok) and `llms.txt` |")
    lines.extend([
        "",
        "## Prioritized Action Plan",
        "1. Keep always-on root instructions (`AGENTS.md` / `CLAUDE.md`) under **800 tokens**; move long specs to on-demand reference files.",
        "2. Minify/prune verbose JSON tool/OpenAPI parameter descriptions — redundant parameter examples add 20–45% recurring prompt overhead per turn.",
        "3. Deduplicate repeated policy bullets across `README.md`, `AGENTS.md`, and `SKILL.md` by linking to one canonical source.",
    ])
    return "\n".join(lines)


# ============================================================================
# 2. TAN CodeGuard Scanner Handlers
# ============================================================================

SECURITY_RULES: list[tuple[str, str, str, re.Pattern[str], str]] = [
    ("CRIT", "SECRET-KEY", "Possible hardcoded private key / API secret",
     re.compile(r"(?:private_?key|secret_?key|api_?key|bearer)\s*[:=]\s*['\"](?:0x[0-9a-fA-F]{64}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,})['\"]", re.I),
     "Move secret to environment variable or chmod-0600 keystore; never commit credentials."),
    ("HIGH", "SOL-REENTRANCY", "External call (.call/.send/.transfer) — check Checks-Effects-Interactions",
     re.compile(r"\.call(?:\{[^}]*\})?\s*\("),
     "Update state balances BEFORE external calls and guard with OpenZeppelin ReentrancyGuard."),
    ("HIGH", "SOL-TXORIGIN", "Use of tx.origin for authorization (SWC-115)",
     re.compile(r"\btx\.origin\b"),
     "Replace tx.origin with msg.sender to prevent phishing contract authorization bypass."),
    ("HIGH", "SOL-DELEGATECALL", "delegatecall usage — verify target immutability & storage layout",
     re.compile(r"\.delegatecall\s*\("),
     "Ensure delegatecall target is immutable or strictly access-controlled and storage slots match."),
    ("HIGH", "SOL-SELFDESTRUCT", "Deprecated/dangerous selfdestruct call",
     re.compile(r"\bselfdestruct\s*\("),
     "Remove selfdestruct; under EIP-6780 it no longer deletes code post-creation tx."),
    ("HIGH", "INJ-EVAL", "Dynamic code execution via eval()/exec()/new Function()",
     re.compile(r"\b(?:eval|exec)\s*\(|new\s+Function\s*\("),
     "Replace dynamic code execution with safe structured parsers or explicit dispatch tables."),
    ("HIGH", "INJ-SHELL", "Shell command execution with shell=True or interpolated command string",
     re.compile(r"subprocess\.(?:run|Popen|call|check_output)\([^)]*shell\s*=\s*True|os\.system\s*\(|child_process\.exec\s*\("),
     "Pass argument lists with shell=False / execFile to prevent command injection."),
    ("HIGH", "INJ-SQL", "Possible SQL string interpolation / concatenation",
     re.compile(r"(?:SELECT|INSERT|UPDATE|DELETE)\b[^\n]{0,80}(?:\+\s*[a-zA-Z_]|\$\{|f['\"])", re.I),
     "Use parameterized queries / prepared statements instead of string interpolation."),
    ("HIGH", "DESER-PICKLE", "Unsafe deserialization (pickle.loads / yaml.load without SafeLoader)",
     re.compile(r"\bpickle\.loads?\s*\(|\byaml\.load\s*\([^)]*\)"),
     "Use json or yaml.safe_load(); never unpickle untrusted bytes."),
    ("MED", "XSS-INNERHTML", "Direct DOM HTML injection (innerHTML / dangerouslySetInnerHTML)",
     re.compile(r"\.innerHTML\s*=|dangerouslySetInnerHTML"),
     "Use textContent or sanitize HTML strictly with DOMPurify before rendering."),
    ("MED", "SOL-UNCHECKED", "Solidity unchecked arithmetic block — verify overflow safety",
     re.compile(r"\bunchecked\s*\{"),
     "Document mathematical invariant proving no overflow/underflow is possible inside unchecked{}."),
    ("MED", "PY-BARE-EXCEPT", "Bare except: swallows KeyboardInterrupt/SystemExit and hides bugs",
     re.compile(r"^\s*except\s*:\s*$"),
     "Catch explicit exception types (e.g., except ValueError:) and log failures."),
    ("MED", "CRYPTO-WEAK-HASH", "Weak cryptographic hash (MD5/SHA1) detected",
     re.compile(r"\b(?:hashlib\.(?:md5|sha1)|createHash\(['\"](?:md5|sha1)['\"]\))"),
     "Use SHA-256/SHA-3 for integrity and Argon2/scrypt for passwords."),
    ("LOW", "TLS-NOVERIFY", "TLS certificate verification disabled (verify=False / rejectUnauthorized: false)",
     re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false"),
     "Enable strict TLS verification in production to prevent MITM attacks."),
    ("LOW", "TODO-FIXME", "Unresolved FIXME/HACK/XXX marker in code",
     re.compile(r"\b(?:FIXME|HACK|XXX)\b"),
     "Resolve or link tracked issue before shipping to production."),
]


def run_codeguard_security_scan(req_text: str) -> str:
    content = (req_text or "").strip()
    source_note = "inline code"
    if len(content) < 300:
        url = _extract_first_url(content)
        if url:
            # Convert github.com/.../blob/... to raw.githubusercontent.com
            m = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", url)
            fetch_url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}" if m else url
            fetched = _http_fetch(fetch_url)
            if fetched["ok"] and fetched["body"]:
                content = fetched["body"]
                source_note = f"fetched {fetch_url} ({fetched['bytes']} bytes)"

    if not content:
        return "# TAN CodeGuard — Error\nNo source code or public raw file URL provided in requirements."

    lines = content.splitlines()
    findings: list[dict[str, Any]] = []
    for idx, ln in enumerate(lines, 1):
        for sev, rule_id, title, pat, fix in SECURITY_RULES:
            if pat.search(ln):
                findings.append({
                    "severity": sev,
                    "rule": rule_id,
                    "line": idx,
                    "title": title,
                    "snippet": ln.strip()[:120],
                    "fix": fix,
                })

    sev_order = {"CRIT": 0, "HIGH": 1, "MED": 2, "LOW": 3}
    findings.sort(key=lambda x: (sev_order.get(x["severity"], 9), x["line"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("CRIT", "HIGH", "MED", "LOW")}
    verdict = "FAIL" if (counts["CRIT"] or counts["HIGH"]) else ("WARN" if counts["MED"] else "PASS")

    out = [
        "# TAN CodeGuard — Deterministic Static Security & Bug Scan",
        f"- **Timestamp (UTC):** {utc_now()}",
        f"- **Source:** {source_note} ({len(lines):,} lines, {len(content):,} bytes)",
        f"- **Verdict:** **{verdict}** (CRIT: {counts['CRIT']} | HIGH: {counts['HIGH']} | MED: {counts['MED']} | LOW: {counts['LOW']})",
        f"- **Scope Note:** Automated deterministic rule scan (15 security/reliability rules). Does not replace full manual audit or fuzzing.",
        "",
    ]
    if not findings:
        out.append("## Findings\nNo static rule violations matched across the 15 checked security/reliability patterns.")
    else:
        out.append("## Findings")
        for f in findings[:40]:
            out.append(f"### [{f['severity']}] `{f['rule']}` (Line {f['line']}) — {f['title']}")
            out.append(f"- **Code:** `{f['snippet']}`")
            out.append(f"- **Recommended Fix:** {f['fix']}")
    return "\n".join(out)


def run_codeguard_repo_audit(req_text: str) -> str:
    gh_repo = _extract_github_repo(req_text or "")
    if not gh_repo:
        return run_codeguard_security_scan(req_text)
    owner, repo = gh_repo
    meta = _http_fetch(f"https://api.github.com/repos/{owner}/{repo}")
    if not meta["ok"]:
        return f"# TAN CodeGuard Repo Audit — Error\nFailed to fetch `{owner}/{repo}`: {meta.get('error')}."
    info = json.loads(meta["body"])
    branch = info.get("default_branch", "main")
    tree_res = _http_fetch(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1", max_bytes=300_000)
    paths = [x["path"] for x in json.loads(tree_res["body"]).get("tree", []) if x.get("type") == "blob"] if tree_res["ok"] else []
    low = {p.lower(): p for p in paths}

    checks = [
        ("License", bool(info.get("license")), (info.get("license") or {}).get("spdx_id", "missing")),
        ("README", any(k.startswith("readme") for k in low), "present" if any(k.startswith("readme") for k in low) else "missing"),
        ("SECURITY.md", "security.md" in low or ".github/security.md" in low, "present" if ("security.md" in low or ".github/security.md" in low) else "missing"),
        ("CI Workflows", any(k.startswith(".github/workflows/") for k in low), f"{sum(1 for k in low if k.startswith('.github/workflows/'))} workflow(s)"),
        ("Automated Tests", any("test" in k for k in low), f"{sum(1 for k in low if 'test' in k)} test file(s)"),
        ("Lockfile Pinning", any(k.endswith(("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.lock", "poetry.lock", "uv.lock", "go.sum")) for k in low), "pinned" if any(k.endswith(("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.lock", "poetry.lock", "uv.lock", "go.sum")) for k in low) else "no lockfile"),
        ("Agent Readiness (AGENTS.md / llms.txt)", any(k.endswith(("agents.md", "llms.txt", "skill.md")) for k in low), "present" if any(k.endswith(("agents.md", "llms.txt", "skill.md")) for k in low) else "missing"),
        ("No Committed .env", not any(k == ".env" or k.endswith("/.env") for k in low), "clean" if not any(k == ".env" or k.endswith("/.env") for k in low) else "WARNING: .env in tree!"),
    ]
    score = sum(1 for _, ok, _ in checks if ok)
    out = [
        f"# TAN CodeGuard — GitHub Repository Readiness Audit: `{owner}/{repo}`",
        f"- **Timestamp (UTC):** {utc_now()}",
        f"- **Stars:** {info.get('stargazers_count', 0)} | **Open Issues:** {info.get('open_issues_count', 0)} | **Language:** {info.get('language') or 'unknown'} | **Last Push:** {info.get('pushed_at')}",
        f"- **Readiness Score:** **{score}/{len(checks)}**",
        "",
        "## Hygiene & Security Checklist",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        out.append(f"| {name} | {'PASS' if ok else 'WARN'} | {detail} |")
    return "\n".join(out)


# ============================================================================
# 3. TAN ChainCheck Verifier Handlers
# ============================================================================

def _eth_rpc(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "TAN-ChainCheck/1.0"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data["result"]


def run_chaincheck_address(req_text: str) -> str:
    txt = (req_text or "").strip()
    m = re.search(r"0x[0-9a-fA-F]{40}", txt)
    if not m:
        return "# TAN ChainCheck — Error\nNo valid 40-hex EVM address (`0x...`) found in requirements."
    addr = m.group(0)
    low = txt.lower()
    chain = "base"
    for c in ("ethereum", "eth", "mainnet", "optimism", "arbitrum", "base"):
        if re.search(rf"\b{c}\b", low):
            chain = "ethereum" if c in ("eth", "mainnet") else c
            break
    rpc_url, usdc_addr = CHAIN_RPCS[chain]

    try:
        bal_hex = _eth_rpc(rpc_url, "eth_getBalance", [addr, "latest"])
        nonce_hex = _eth_rpc(rpc_url, "eth_getTransactionCount", [addr, "latest"])
        code_hex = _eth_rpc(rpc_url, "eth_getCode", [addr, "latest"])
        call_data = "0x70a08231" + addr.lower().replace("0x", "").zfill(64)
        usdc_hex = _eth_rpc(rpc_url, "eth_call", [{"to": usdc_addr, "data": call_data}, "latest"])
        impl_raw = _eth_rpc(rpc_url, "eth_getStorageAt", [addr, EIP1967_IMPL_SLOT, "latest"])
        admin_raw = _eth_rpc(rpc_url, "eth_getStorageAt", [addr, EIP1967_ADMIN_SLOT, "latest"])
        zos_raw = _eth_rpc(rpc_url, "eth_getStorageAt", [addr, ZOS_IMPL_SLOT, "latest"])
    except Exception as exc:
        return f"# TAN ChainCheck — RPC Error\nLive RPC query on `{chain}` failed: {exc}"

    eth_bal = int(bal_hex, 16) / 1e18
    usdc_bal = int(usdc_hex, 16) / 1e6 if usdc_hex and usdc_hex != "0x" else 0.0
    nonce = int(nonce_hex, 16)
    code_bytes = (len(code_hex) - 2) // 2 if code_hex and code_hex.startswith("0x") else 0
    acct_type = "Smart Contract" if code_bytes > 0 else "EOA (Externally Owned Account)"

    def slot_to_addr(raw: str) -> str | None:
        if not raw or not raw.startswith("0x"):
            return None
        val = int(raw, 16)
        if val == 0:
            return None
        return "0x" + raw[-40:]

    impl_addr = slot_to_addr(impl_raw)
    zos_addr = slot_to_addr(zos_raw)
    admin_addr = slot_to_addr(admin_raw)
    proxy_impl = impl_addr or zos_addr
    proxy_kind = "EIP-1967" if impl_addr else ("ZeppelinOS/FiatTokenProxy" if zos_addr else None)

    result = {
        "timestamp_utc": utc_now(),
        "chain": chain,
        "rpc_source": rpc_url,
        "address": addr,
        "account_type": acct_type,
        "eth_balance": round(eth_bal, 8),
        "usdc_balance": round(usdc_bal, 6),
        "usdc_contract": usdc_addr,
        "nonce": nonce,
        "bytecode_bytes": code_bytes,
        "proxy_standard": proxy_kind,
        "proxy_implementation": proxy_impl,
        "eip1967_admin": admin_addr,
        "is_proxy": bool(proxy_impl),
    }
    return (
        f"# TAN ChainCheck — Live On-Chain Snapshot (`{chain}`)\n\n"
        f"- **Address:** `{addr}` ({acct_type})\n"
        f"- **ETH Balance:** `{eth_bal:.6f} ETH`\n"
        f"- **USDC Balance:** `${usdc_bal:,.6f} USDC`\n"
        f"- **Nonce:** `{nonce}` | **Bytecode Size:** `{code_bytes:,} bytes`\n"
        f"- **Upgradeable Proxy:** `{proxy_kind + ' -> ' + proxy_impl if proxy_impl else 'No'}`"
        + (f" (Admin: `{admin_addr}`)" if admin_addr else "")
        + "\n\n```json\n"
        + json.dumps(result, indent=2)
        + "\n```"
    )


def run_chaincheck_endpoint(req_text: str) -> str:
    txt = (req_text or "").strip()
    url = _extract_first_url(txt)
    if not url:
        # Allow bare domain like example.com
        m = re.search(r"\b([a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)\b", txt)
        if m:
            url = "https://" + m.group(1)
    if not url:
        return "# TAN ChainCheck Endpoint Audit — Error\nNo public HTTPS URL or domain found in requirements."

    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    main_res = _http_fetch(url)
    llms_res = _http_fetch(origin + "/llms.txt")
    x402_res = _http_fetch(origin + "/.well-known/x402")
    card_res = _http_fetch(origin + "/.well-known/agent-card.json")
    robots_res = _http_fetch(origin + "/robots.txt")

    has_402 = main_res["status"] == 402 or any(
        k in main_res.get("headers", {}) for k in ("payment-required", "x-payment-required", "www-authenticate")
    )
    h = main_res.get("headers", {})
    sec_headers = [k for k in ("strict-transport-security", "content-security-policy", "x-content-type-options") if k in h]

    checks = [
        ("Target Reachable", main_res["status"] in (200, 201, 204, 402), f"HTTP {main_res['status']} in {main_res['latency_ms']}ms"),
        ("llms.txt", llms_res["status"] == 200 and len(llms_res["body"]) > 20, f"HTTP {llms_res['status']} ({llms_res.get('bytes', 0)} B)"),
        (".well-known/x402", x402_res["status"] == 200, f"HTTP {x402_res['status']} ({x402_res.get('bytes', 0)} B)"),
        (".well-known/agent-card.json", card_res["status"] == 200, f"HTTP {card_res['status']} ({card_res.get('bytes', 0)} B)"),
        ("robots.txt", robots_res["status"] == 200, f"HTTP {robots_res['status']}"),
        ("HTTP 402 / Payment Header", has_402 or x402_res["status"] == 200, "detected" if (has_402 or x402_res["status"] == 200) else "not advertised"),
        ("Security Headers", len(sec_headers) > 0, ", ".join(sec_headers) if sec_headers else "none"),
    ]
    passed = sum(1 for _, ok, _ in checks if ok)

    lines = [
        f"# TAN ChainCheck — Live Endpoint & Agent-Readiness Audit",
        f"- **Timestamp (UTC):** {utc_now()}",
        f"- **Target URL:** `{url}`",
        f"- **Origin:** `{origin}`",
        f"- **Score:** **{passed}/{len(checks)}**",
        "",
        "| Check | Result | Live Probe Detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'PASS' if ok else 'WARN'} | {detail} |")
    return "\n".join(lines)


HANDLERS = {
    "tokendiet_prompt_audit": run_tokendiet_prompt_audit,
    "tokendiet_repo_audit": run_tokendiet_repo_audit,
    "codeguard_security_scan": run_codeguard_security_scan,
    "codeguard_repo_audit": run_codeguard_repo_audit,
    "chaincheck_address": run_chaincheck_address,
    "chaincheck_endpoint": run_chaincheck_endpoint,
}


def dispatch_service(handler_key: str, requirements: str) -> str:
    fn = HANDLERS.get(handler_key)
    if not fn:
        return f"# TAN Provider Error\nUnknown handler key `{handler_key}`."
    try:
        return fn(requirements)
    except Exception as exc:
        log.exception("Handler %s failed", handler_key)
        return f"# TAN Provider Execution Error\nHandler `{handler_key}` encountered an internal error: {exc}"


def append_order_log(entry: dict[str, Any]) -> None:
    ORDER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ORDER_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": utc_now(), **entry}) + "\n")


# ============================================================================
# Live CROO Multi-Agent Provider Runtime
# ============================================================================

def load_agents_config() -> list[dict[str, Any]]:
    if not SECRETS_PATH.exists():
        return []
    data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    return data.get("agents", [])


async def handle_agent_sweep(client: Any, agent_cfg: dict[str, Any], delivered_ids: set[str], accepted_negs: set[str]) -> dict[str, int]:
    from croo import DeliverOrderRequest, DeliverableType, ListOptions

    svc_map: dict[str, str] = agent_cfg.get("service_handlers", {})
    default_handler = next(iter(svc_map.values()), "tokendiet_prompt_audit")
    stats = {"negs_accepted": 0, "orders_delivered": 0, "orders_seen": 0}

    # 1. Sweep pending negotiations
    opts = ListOptions(role="provider", agent_id=agent_cfg["agent_id"], page=1, page_size=20)
    try:
        negs = await client.list_negotiations(opts)
        items = negs.items if hasattr(negs, "items") else (negs if isinstance(negs, list) else [])
        for neg in items:
            nid = getattr(neg, "negotiation_id", "")
            st = str(getattr(neg, "status", "")).lower()
            if nid and st in ("pending", "created", "negotiating") and nid not in accepted_negs:
                log.info("[%s] Accepting pending negotiation %s", agent_cfg["name"], nid)
                try:
                    res = await client.accept_negotiation(nid)
                    accepted_negs.add(nid)
                    stats["negs_accepted"] += 1
                    order_obj = getattr(res, "order", None)
                    append_order_log({
                        "event": "negotiation_accepted",
                        "agent": agent_cfg["name"],
                        "negotiation_id": nid,
                        "order_id": getattr(order_obj, "order_id", getattr(res, "order_id", "")),
                    })
                except Exception as exc:
                    log.warning("[%s] accept_negotiation(%s) err: %s", agent_cfg["name"], nid, exc)
    except Exception as exc:
        log.warning("[%s] list_negotiations err: %s", agent_cfg["name"], exc)

    # 2. Sweep paid orders ready for delivery
    try:
        orders = await client.list_orders(opts)
        items = orders.items if hasattr(orders, "items") else (orders if isinstance(orders, list) else [])
        stats["orders_seen"] = len(items)
        for order in items:
            oid = getattr(order, "order_id", "")
            st = str(getattr(order, "status", "")).lower()
            if oid and st in ("paid", "locked", "in_progress") and oid not in delivered_ids:
                nid = getattr(order, "negotiation_id", "")
                sid = getattr(order, "service_id", "")
                req_text = getattr(order, "requirements", "") or ""
                if nid and not req_text:
                    try:
                        neg = await client.get_negotiation(nid)
                        req_text = getattr(neg, "requirements", "") or ""
                    except Exception as exc:
                        log.warning("[%s] get_negotiation(%s) err: %s", agent_cfg["name"], nid, exc)
                handler_key = svc_map.get(sid, default_handler)
                log.info("[%s] Fulfilling paid order %s via handler %s", agent_cfg["name"], oid, handler_key)
                output_text = await asyncio.to_thread(dispatch_service, handler_key, req_text)
                try:
                    await client.deliver_order(
                        oid,
                        DeliverOrderRequest(
                            deliverable_type=DeliverableType.TEXT,
                            deliverable_text=output_text,
                        ),
                    )
                    delivered_ids.add(oid)
                    stats["orders_delivered"] += 1
                    append_order_log({
                        "event": "order_delivered",
                        "agent": agent_cfg["name"],
                        "order_id": oid,
                        "service_id": sid,
                        "handler": handler_key,
                        "price": getattr(order, "price", ""),
                        "output_chars": len(output_text),
                    })
                    log.info("[%s] Delivered order %s (%d chars)", agent_cfg["name"], oid, len(output_text))
                except Exception as exc:
                    log.error("[%s] deliver_order(%s) err: %s", agent_cfg["name"], oid, exc)
    except Exception as exc:
        log.debug("[%s] list_orders err: %s", agent_cfg["name"], exc)

    return stats


async def run_agent_loop(agent_cfg: dict[str, Any], stop_event: asyncio.Event) -> None:
    from croo import AgentClient, Config, EventType

    client = AgentClient(
        Config(base_url=CROO_API_URL, ws_url=CROO_WS_URL, rpc_url="https://mainnet.base.org"),
        agent_cfg["sdk_key"],
    )
    delivered_ids: set[str] = set()
    accepted_negs: set[str] = set()

    stream = None
    try:
        stream = await client.connect_websocket()
        log.info("[%s] WebSocket connected (agent_id=%s)", agent_cfg["name"], agent_cfg["agent_id"])

        def on_event(e: Any) -> None:
            log.info("[%s] WS event type=%s order=%s neg=%s", agent_cfg["name"], getattr(e, "type", ""), getattr(e, "order_id", ""), getattr(e, "negotiation_id", ""))
            asyncio.create_task(handle_agent_sweep(client, agent_cfg, delivered_ids, accepted_negs))

        stream.on(EventType.NEGOTIATION_CREATED, on_event)
        stream.on(EventType.ORDER_PAID, on_event)
        stream.on(EventType.ORDER_COMPLETED, lambda e: append_order_log({
            "event": "order_completed",
            "agent": agent_cfg["name"],
            "order_id": getattr(e, "order_id", ""),
        }))
    except Exception as exc:
        log.warning("[%s] WS initial connect failed (%s); falling back to HTTP polling", agent_cfg["name"], exc)

    try:
        while not stop_event.is_set():
            await handle_agent_sweep(client, agent_cfg, delivered_ids, accepted_negs)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=45.0)
            except asyncio.TimeoutError:
                pass
    finally:
        if stream:
            try:
                await stream.close()
            except Exception:
                pass
        await client.close()


async def run_check_once() -> int:
    from croo import AgentClient, Config

    agents = load_agents_config()
    if not agents:
        print(json.dumps({"ok": False, "error": "no croo agents configured"}))
        return 1
    summary = []
    for ag in agents:
        client = AgentClient(Config(base_url=CROO_API_URL, ws_url=CROO_WS_URL, rpc_url="https://mainnet.base.org"), ag["sdk_key"])
        try:
            st = await handle_agent_sweep(client, ag, set(), set())
            summary.append({"name": ag["name"], "agent_id": ag["agent_id"], **st})
        finally:
            await client.close()
    print(json.dumps({"ok": True, "ts_utc": utc_now(), "agents": summary}))
    return 0


async def run_daemon() -> int:
    agents = load_agents_config()
    if not agents:
        log.error("No agents found in %s", SECRETS_PATH)
        return 1
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    log.info("Starting CROO multi-agent provider for %d agent(s)", len(agents))
    await asyncio.gather(*(run_agent_loop(ag, stop_event) for ag in agents))
    return 0


def run_self_test() -> int:
    tests = [
        ("tokendiet_prompt_audit", "Please note that in order to get the best results, make sure that you always check the input.\n\n\nPlease note that in order to get the best results, make sure that you always check the input.\nAt this point in time utilize clean JSON."),
        ("codeguard_security_scan", "function withdraw() public {\n  (bool ok,) = msg.sender.call{value: bal[msg.sender]}(\"\");\n  require(tx.origin == owner);\n  bal[msg.sender] = 0;\n}"),
        ("chaincheck_address", "Check 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 on base"),
        ("chaincheck_endpoint", "https://api.croo.network/backend/v1/public/tags"),
        ("tokendiet_repo_audit", "20171806011/tan-tokendiet"),
        ("codeguard_repo_audit", "20171806011/tan-tokendiet"),
    ]
    for key, sample in tests:
        t0 = time.monotonic()
        res = dispatch_service(key, sample)
        ms = int((time.monotonic() - t0) * 1000)
        ok = len(res) > 100 and "Execution Error" not in res
        print(f"[{'PASS' if ok else 'FAIL'}] {key:26s} {ms:4d}ms len={len(res):5d} | {res.splitlines()[0][:70]}")
        if not ok:
            print(res[:500])
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--check-once", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return run_self_test()
    if args.check_once:
        return asyncio.run(run_check_once())
    return asyncio.run(run_daemon())


if __name__ == "__main__":
    sys.exit(main())
