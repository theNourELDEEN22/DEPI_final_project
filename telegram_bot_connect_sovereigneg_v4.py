"""@Retail_ops_bot — AI Retail Operations Agent + Live Supabase + On-Demand PDF Bot (Epics A-E).

Key Capabilities (v4 — Full Pagination, Real Date Windows & True COGS):
1. Accurate Live Supabase Querying (Dynamic Schema Discovery & Server-Side Sorting):
   - Auto-inspects actual table columns (`limit=1`) before querying so it never hits
     HTTP 400 column-mismatch errors.
   - Prioritizes real business transaction dates (`sold_at`, `sale_date`, `transaction_date`,
     etc.) and transaction IDs over CSV upload timestamps (`created_at`).
   - Uses server-side `order=...desc` to fetch the TRUE latest rows across large tables
     (370k+ rows), completes every item in multi-item receipts, and resolves real product
     names via targeted `product_id=in.(...)` lookups.
2. Conversation Memory & Accurate Follow-Up Counts:
   - Remembers the last 6 chat turns within the active branch (cleared on branch switch
     for Branch Isolation A6) so follow-ups like "Now what about the 3 orders precede it"
     accurately return all 3 preceding receipts.
3. Automatic Executive PDF Generation for Database Queries:
   - Whenever you ask for data from the database (receipts, orders, products, sales,
     returns, offers, audit trail), the bot compiles the live records into a clean
     multi-column A4 PDF alongside the AI summary and sends the `.pdf` directly in chat.
4. Clean Telegram Formatting & White-Label Branding:
   - Strips raw `**` Markdown asterisks before sending to Telegram.
   - Shows clean `Retail AI (gpt-4o-mini)` branding across messages and PDFs.
"""
from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import io
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from foundation.failure_test_harness import DuplicateSchedulerAndStaleDataHarness
from foundation.langgraph_runtime import OutboxMessage
from foundation.staging_deploy import (
    PILOT_INVENTORY_SEED,
    PILOT_SUPPLIERS_SEED,
)
from foundation.validation_retry_middleware import InputValidationError, InputValidator


# ---------------------------------------------------------------------------
# 0. Built-in .env Loader (Zero external dependencies)
# ---------------------------------------------------------------------------
def load_dotenv_file(env_path: Path = Path(".env")) -> None:
    """Loads KEY=VALUE pairs from `.env` into os.environ without overwriting existing vars."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


load_dotenv_file()

DEFAULT_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "",
)
CHAT_ID_CACHE_FILE = Path(".telegram_chat_id")
GENERATED_REPORTS_DIR = Path("generated_reports")

FALLBACK_BRANCH_META: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "Maadi Branch",
        "code": "BR-MAADI",
        "city": "Cairo",
        "approver": "stock.maadi@supermarket.eg",
        "report_recipient": "report.maadi@supermarket.eg",
        "operator": "ops.maadi@supermarket.eg",
        "gross_sales": Decimal("18750.00"),
        "net_revenue": Decimal("18200.00"),
        "gross_profit": Decimal("5460.00"),
        "margin_pct": Decimal("30.0"),
        "transactions": 142,
        "units_sold": 615,
        "atv": Decimal("128.17"),
        "offer_redemptions": 18,
        "offer_discount_total": Decimal("550.00"),
        "offer_id": 501,
        "offer_title": "Near-Expiry Dairy Clearance",
        "offer_price": Decimal("24.50"),
        "discount_pct": Decimal("16.2"),
    },
    2: {
        "name": "Nasr City Branch",
        "code": "BR-NASR",
        "city": "Cairo",
        "approver": "stock.nasrcity@supermarket.eg",
        "report_recipient": "report.nasrcity@supermarket.eg",
        "operator": "ops.nasrcity@supermarket.eg",
        "gross_sales": Decimal("24300.00"),
        "net_revenue": Decimal("23450.00"),
        "gross_profit": Decimal("7269.50"),
        "margin_pct": Decimal("31.0"),
        "transactions": 189,
        "units_sold": 810,
        "atv": Decimal("124.07"),
        "offer_redemptions": 27,
        "offer_discount_total": Decimal("850.00"),
        "offer_id": 502,
        "offer_title": "Fresh Bakery Same-Day Promo",
        "offer_price": Decimal("31.00"),
        "discount_pct": Decimal("19.5"),
    },
    3: {
        "name": "6th of October Branch",
        "code": "BR-OCT",
        "city": "Giza",
        "approver": "stock.october@supermarket.eg",
        "report_recipient": "report.october@supermarket.eg",
        "operator": "ops.october@supermarket.eg",
        "gross_sales": Decimal("31900.00"),
        "net_revenue": Decimal("30800.00"),
        "gross_profit": Decimal("9856.00"),
        "margin_pct": Decimal("32.0"),
        "transactions": 215,
        "units_sold": 940,
        "atv": Decimal("143.26"),
        "offer_redemptions": 34,
        "offer_discount_total": Decimal("1100.00"),
        "offer_id": 503,
        "offer_title": "Frozen Food Bundle Clearance",
        "offer_price": Decimal("145.00"),
        "discount_pct": Decimal("12.1"),
    },
}

PRODUCTS_BY_ID: Dict[int, Dict[str, Any]] = {
    int(item["product_id"]): dict(item) for item in PILOT_INVENTORY_SEED
}
SUPPLIERS_BY_ID: Dict[int, Dict[str, Any]] = {
    int(sup["supplier_id"]): dict(sup) for sup in PILOT_SUPPLIERS_SEED
}


def _money(val: Any) -> Decimal:
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def strip_markdown_formatting(text: str) -> str:
    """Removes raw markdown tokens (`**`, `__`, `###`, backticks) so Telegram renders cleanly."""
    cleaned = str(text)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s+", "", cleaned)
    return cleaned.strip()


def normalize_chat_completions_url(raw_url: str) -> str:
    """Ensures base URLs ending in `/v1` point to `/v1/chat/completions`."""
    cleaned = raw_url.strip().rstrip("/")
    if not cleaned:
        return "https://backend.sovereigneg.com/v1/chat/completions"
    if cleaned.endswith("/chat/completions"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/chat/completions"
    return f"{cleaned}/v1/chat/completions"


def is_casual_smalltalk(text: str) -> bool:
    """Returns True only for short greetings/thanks that do not request database records."""
    low = re.sub(r"[^\w\s]", "", text.lower()).strip()
    return low in {
        "hi", "hello", "hey", "ok", "okay", "thanks", "thank you", "thx",
        "good", "great", "nice", "cool", "awesome", "bye", "goodbye",
        "shokran", "تمام", "شكرا", "اهلا", "سلام",
    }


def has_non_latin_chars(text: str) -> bool:
    """Returns True if text contains Arabic or other non-Latin letters (ignoring punctuation like em-dashes)."""
    return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]", str(text)))


def _normalize_arabic_digits(text: str) -> str:
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    return text.translate(trans)


def extract_explicit_count(text: str, max_count: int = 25) -> Optional[int]:
    """Detects an explicit record count in English or Arabic (e.g., 'last 4', 'اربع فواتير')."""
    norm = _normalize_arabic_digits(text.lower())
    word_nums = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "واحد": 1, "واحدة": 1, "اتنين": 2, "اثنين": 2, "اثنان": 2,
        "تلاتة": 3, "تلات": 3, "ثلاثة": 3, "ثلاث": 3,
        "اربع": 4, "أربع": 4, "اربعة": 4, "أربعة": 4,
        "خمس": 5, "خمسة": 5, "ست": 6, "ستة": 6,
        "سبع": 7, "سبعة": 7, "تمان": 8, "تمانية": 8, "ثمان": 8, "ثمانية": 8,
        "تسع": 9, "تسعة": 9, "عشر": 10, "عشرة": 10,
    }
    matches: List[int] = []
    for w, n in word_nums.items():
        if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", norm):
            matches.append(n)
    for m in re.findall(r"(?<!\w)(\d{1,2})(?!\w)", norm):
        n = int(m)
        if 1 <= n <= max_count:
            matches.append(n)
    return max(matches) if matches else None


def extract_requested_count(text: str, default_count: int = 10, max_count: int = 25) -> int:
    """Computes how many receipts to fetch (requested count + buffer for follow-ups)."""
    explicit = extract_explicit_count(text, max_count=max_count)
    if explicit is None:
        return default_count
    return min(max_count, max(default_count, explicit + 4))


def parse_sales_period_request(text: str) -> Optional[Dict[str, Any]]:
    """Detects if the user is asking for a period-based sales report (daily, last month, etc.)."""
    norm = _normalize_arabic_digits(text.lower()).strip()

    # Must mention sales/revenue/report/profit/kpi in English or Arabic
    sales_intent_keywords = (
        "sales report", "revenue report", "monthly report", "daily report", "weekly report",
        "sales summary", "total sales", "gross sales", "net revenue", "profit report",
        "تقرير مبيعات", "تقرير المبيعات", "مبيعات الشهر", "مبيعات امبارح", "مبيعات النهارده",
        "مبيعات الاسبوع", "اجمالي المبيعات", "ملخص المبيعات",
    )
    has_sales_word = any(k in norm for k in sales_intent_keywords) or (
        any(w in norm for w in ("sales", "revenue", "مبيعات", "ايرادات"))
        and any(w in norm for w in ("report", "summary", "total", "month", "week", "yesterday", "today", "تقرير", "شهر", "اسبوع", "امبارح", "النهارده"))
    )
    # Do NOT hijack receipt/invoice-specific requests
    if any(w in norm for w in ("receipt", "invoice", "فاتور", "فواتير", "ايصال", "إيصال")):
        return None
    if not has_sales_word:
        return None

    # 1. Explicit YYYY-MM
    ym_match = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", norm)
    if ym_match:
        return {"kind": "month", "year": int(ym_match.group(1)), "month": int(ym_match.group(2))}

    # 2. Relative periods (English + Arabic)
    if any(k in norm for k in ("last month", "previous month", "past month", "الشهر اللي فات", "الشهر الماضي", "الشهر السابق")):
        return {"kind": "last_month"}
    if any(k in norm for k in ("this month", "current month", "الشهر ده", "الشهر الحالي", "هذا الشهر")):
        return {"kind": "this_month"}
    if any(k in norm for k in ("yesterday", "امبارح", "امس", "أمس")):
        return {"kind": "yesterday"}
    if any(k in norm for k in ("last week", "past week", "last 7 days", "weekly", "الاسبوع اللي فات", "الأسبوع الماضي", "اخر اسبوع", "اسبوعي")):
        return {"kind": "last_7_days"}
    if any(k in norm for k in ("last 30 days", "past 30 days", "اخر 30 يوم")):
        return {"kind": "last_30_days"}
    if any(k in norm for k in ("all time", "all-time", "overall", "entire database", "كل المبيعات", "الاجمالي الكلي")):
        return {"kind": "all_time"}
    if any(k in norm for k in ("today", "daily", "النهارده", "اليوم", "يومي")):
        return {"kind": "daily"}

    # 3. Named month (English + Arabic)
    month_map = {
        "january": 1, "jan": 1, "يناير": 1,
        "february": 2, "feb": 2, "فبراير": 2,
        "march": 3, "mar": 3, "مارس": 3,
        "april": 4, "apr": 4, "ابريل": 4, "أبريل": 4,
        "may": 5, "مايو": 5,
        "june": 6, "jun": 6, "يونيو": 6,
        "july": 7, "jul": 7, "يوليو": 7,
        "august": 8, "aug": 8, "اغسطس": 8, "أغسطس": 8,
        "september": 9, "sep": 9, "sept": 9, "سبتمبر": 9,
        "october": 10, "oct": 10, "اكتوبر": 10, "أكتوبر": 10,
        "november": 11, "nov": 11, "نوفمبر": 11,
        "december": 12, "dec": 12, "ديسمبر": 12,
    }
    for name, m_num in month_map.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", norm):
            yr_match = re.search(r"\b(20\d{2})\b", norm)
            yr = int(yr_match.group(1)) if yr_match else datetime.now(timezone.utc).year
            return {"kind": "month", "year": yr, "month": m_num}

    return {"kind": "daily"}


# ---------------------------------------------------------------------------
# 1. Live LLM Agent (OpenAI-Compatible Stdlib HTTP, Clean Branding)
# ---------------------------------------------------------------------------
class RetailAIAgent:
    """Calls the configured OpenAI-compatible LLM for branch reports, offers, and Q&A."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        sov_key = os.environ.get("SOVEREIGNEG_API_KEY", "").strip()
        oai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

        self.api_key = (api_key or sov_key or oai_key or or_key or "").strip()

        raw_base = (
            base_url
            or os.environ.get("SOVEREIGNEG_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or ""
        ).strip()

        if raw_base:
            self.endpoint_url = normalize_chat_completions_url(raw_base)
        elif sov_key or (not self.api_key.startswith("sk-or-") and not oai_key):
            self.endpoint_url = "https://backend.sovereigneg.com/v1/chat/completions"
        elif self.api_key.startswith("sk-or-"):
            self.endpoint_url = "https://openrouter.ai/api/v1/chat/completions"
        else:
            self.endpoint_url = "https://api.openai.com/v1/chat/completions"

        self._is_openrouter = "openrouter.ai" in self.endpoint_url
        self.model = (
            model
            or os.environ.get("SOVEREIGNEG_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or ("openai/gpt-4o-mini" if self._is_openrouter else "gpt-4o-mini")
        ).strip()
        self.display_name = "Retail AI"

    @property
    def is_enabled(self) -> bool:
        return bool(
            self.api_key
            and "PASTE" not in self.api_key.upper()
            and not self.api_key.startswith("YOUR_")
        )

    @property
    def model_badge(self) -> str:
        return f"{self.display_name} ({self.model})"

    def chat_messages(self, messages: List[Dict[str, str]], max_tokens: int = 500) -> Optional[str]:
        """Executes a Chat Completions request with automatic brief retry on HTTP 429."""
        if not self.is_enabled:
            return None

        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "DEPI-RetailOpsBot/1.0",
        }
        if self._is_openrouter:
            headers["HTTP-Referer"] = "https://github.com/theNourELDEEN22/DEPI_final_project"
            headers["X-Title"] = "DEPI Retail AI Agents"

        body_bytes = json.dumps(payload).encode("utf-8")

        for attempt in range(2):
            req = urllib.request.Request(
                self.endpoint_url,
                data=body_bytes,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    raw = body["choices"][0]["message"]["content"].strip()
                    return strip_markdown_formatting(raw)
            except urllib.error.HTTPError as err:
                err_text = ""
                try:
                    err_text = err.read().decode("utf-8", errors="ignore")[:240]
                except Exception:
                    pass
                if err.code == 429 and attempt == 0:
                    print("[AI Agent] HTTP 429 busy — retrying in 2s...")
                    time.sleep(2)
                    continue
                if err.code == 402:
                    print(f"[AI Agent] HTTP 402 Insufficient credit or spend cap reached: {err_text}")
                elif err.code in (400, 404):
                    print(f"[AI Agent] HTTP {err.code} Check model ID '{self.model}': {err_text}")
                else:
                    print(f"[AI Agent] HTTP {err.code} error: {err_text}")
                return None
            except Exception as exc:
                print(f"[AI Agent] Request error (using safe fallback): {exc}")
                return None
        return None

    def chat(self, system_prompt: str, user_prompt: str, max_tokens: int = 420) -> Optional[str]:
        return self.chat_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
        )

    def summarize_sales_report(self, report_data: Dict[str, Any]) -> Tuple[str, str]:
        sys_prompt = (
            "You are a senior Egyptian supermarket retail operations analyst. "
            "Use ONLY the exact numbers provided. Never invent numbers, branches, or emails. "
            "Write 3 concise, numbered executive bullet points (plain text, NO markdown asterisks) "
            "covering: (1) Revenue & Margin health, (2) Promotional Offer impact, and (3) Immediate stock action."
        )
        ai_text = self.chat(sys_prompt, json.dumps(report_data, default=str, ensure_ascii=False), max_tokens=280)
        if ai_text:
            return ai_text, self.model_badge

        fallback = (
            f"1. Margin Health: {report_data['branch_name']} achieved {report_data['net_revenue']} EGP net revenue "
            f"with {report_data['gross_profit']} EGP gross profit ({report_data['margin_pct']}% margin).\n"
            f"2. Promotional Impact: {report_data['offer_redemptions']} offer redemptions contributed "
            f"-{report_data['offer_discount_total']} EGP in controlled clearance discounts.\n"
            f"3. Action Required: Monitor {report_data['flagged_product']} ({report_data['days_until_expiry']}d left) "
            f"and review pending restock approvals before midnight cutoff."
        )
        return fallback, "Rule-Based Fallback"

    def analyze_active_offers(self, offer_data: Dict[str, Any]) -> Tuple[str, str]:
        sys_prompt = (
            "You are an AI Promotional Offer Strategist for a supermarket chain in Egypt. "
            "Use ONLY the exact numbers provided. Do not invent figures. "
            "Write 3 concise, numbered strategic notes (plain text, NO markdown asterisks) "
            "covering: (1) Margin Guardrail Compliance, (2) Expiry Waste Prevention, and (3) Restocking H2 Coordination."
        )
        ai_text = self.chat(sys_prompt, json.dumps(offer_data, default=str, ensure_ascii=False), max_tokens=280)
        if ai_text:
            return ai_text, self.model_badge

        fallback = (
            f"1. Guardrail Compliance: Promotional price of {offer_data['offer_price']} EGP maintains a "
            f"{offer_data['live_margin_pct']}% margin above unit cost ({offer_data['cost_price']} EGP), clearing the 10.0% floor.\n"
            f"2. Waste Reduction: With {offer_data['days_until_expiry']} day(s) until expiry and {offer_data['stock_quantity']} units on shelf, "
            f"the {offer_data['discount_pct']}% discount accelerates sell-through before spoilage.\n"
            f"3. Cross-Workflow Sync (H2): Automated restocking for {offer_data['sku']} remains paused until clearance concludes."
        )
        return fallback, "Rule-Based Fallback"

    def explain_restock_alert(self, branch_name: str, draft: Dict[str, Any], prod: Dict[str, Any]) -> str:
        sys_prompt = (
            "You are a retail inventory restocking assistant. Using ONLY the provided JSON, "
            "write 2 short plain-text sentences explaining why this order is recommended and "
            "what the manager should check before approving. Do not suggest changing recipients or branch."
        )
        user_prompt = json.dumps(
            {
                "branch": branch_name,
                "product": draft.get("product_name"),
                "stock_on_hand": prod.get("stock_quantity"),
                "days_until_expiry": prod.get("days_until_expiry"),
                "demand": prod.get("demand_level"),
                "proposed_qty": draft.get("requested_quantity"),
                "trigger": draft.get("trigger_reason"),
                "lead_days": draft.get("shipping_duration_days"),
            },
            default=str,
        )
        ai_text = self.chat(sys_prompt, user_prompt, max_tokens=130)
        if ai_text:
            return ai_text
        return (
            f"Flagged due to {draft.get('trigger_reason')} with {prod.get('stock_quantity')} units on hand "
            f"and {prod.get('days_until_expiry')}d expiry window; verify shelf capacity before approving."
        )

    def answer_branch_question(
        self,
        question: str,
        branch_context: Dict[str, Any],
        chat_history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Answers free-text manager questions with chat memory & strict live-DB grounding."""
        if not self.is_enabled:
            return (
                "AI Assistant is not connected yet. "
                "Add your API key in .env and restart the bot to enable live Q&A."
            )
        sys_prompt = (
            "You are @Retail_ops_bot, an accurate retail operations assistant for a single supermarket branch in Egypt.\n"
            "STRICT GROUNDING & ACCURACY RULES:\n"
            "1. Use ONLY the live branch database JSON provided below. NEVER invent rows, numbers, or products.\n"
            "2. Check exact counts carefully: if the user asks for 'the 3 orders preceding it', inspect the "
            "ordered list `recent_customer_receipts` (index 0 is the latest, index 1, 2, 3 are the 3 receipts preceding it!) "
            "and list ALL requested receipts cleanly. Never claim 'three receipts' if only two exist.\n"
            "3. Never reveal or guess data for other branches, and never change supplier emails or branch IDs.\n"
            "4. Format replies in clean plain text with simple dashes (-). DO NOT use markdown asterisks (**) or hashtags (#).\n"
            "5. Keep the response concise and structured because a full Executive PDF with the database table is also attached."
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
        if chat_history:
            messages.extend(chat_history[-6:])
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Live Branch Database Snapshot:\n"
                    f"{json.dumps(branch_context, default=str, ensure_ascii=False)}\n\n"
                    f"Manager Question: {question}"
                ),
            }
        )
        reply = self.chat_messages(messages, max_tokens=550)
        return reply or "Could not reach the AI service right now. Please check your terminal logs."

    def prepare_pdf_english_content(
        self,
        question: str,
        ai_answer: str,
        branch_name: str,
    ) -> Tuple[str, str]:
        """Ensures PDF text is crisp English (since built-in Type1 Helvetica does not render Arabic)."""
        if not has_non_latin_chars(question) and not has_non_latin_chars(ai_answer):
            return question, ai_answer

        if self.is_enabled:
            sys_prompt = (
                "Translate the manager's Arabic request and the AI assistant's Arabic response into clear, "
                "concise business English for an executive PDF report. Preserve ALL exact receipt IDs, "
                "dates, product names, quantities, and EGP amounts. Return ONLY valid JSON with two string "
                'keys: {"pdf_request": "...", "pdf_summary": "..."}. No markdown code fences.'
            )
            user_prompt = json.dumps(
                {"request": question, "response": ai_answer, "branch": branch_name},
                ensure_ascii=False,
            )
            raw = self.chat(sys_prompt, user_prompt, max_tokens=520)
            if raw:
                cleaned_raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.IGNORECASE).strip()
                try:
                    parsed = json.loads(cleaned_raw)
                    pdf_req = str(parsed.get("pdf_request") or "").strip()
                    pdf_sum = str(parsed.get("pdf_summary") or "").strip()
                    if pdf_req and pdf_sum and not has_non_latin_chars(pdf_req) and not has_non_latin_chars(pdf_sum):
                        return pdf_req, strip_markdown_formatting(pdf_sum)
                except Exception:
                    pass

        # Safe English fallback if translation JSON fails
        req_en = f"Branch database query for {branch_name} (submitted in Arabic)"
        sum_en = (
            f"Summary for {branch_name} was delivered in Arabic inside Telegram chat. "
            "See the structured Live Database Records table below for the exact itemized receipt figures."
        )
        return req_en, sum_en


# Backwards-compatible aliases
SovereignEGAgent = RetailAIAgent
GPT4oMiniAgent = RetailAIAgent


# ---------------------------------------------------------------------------
# 2. Live Supabase Data Provider (Paginated, Exact Counts, Date-Filtered & Real COGS)
# ---------------------------------------------------------------------------
class LiveBranchDataProvider:
    """Queries live Supabase tables with dynamic column discovery, full pagination, and real date windows."""

    BUSINESS_TIME_COLS = (
        "sold_at",
        "transaction_time",
        "transaction_datetime",
        "transaction_date",
        "sale_datetime",
        "sale_time",
        "sale_date",
        "occurred_at",
        "order_date",
        "business_date",
        "invoice_date",
        "purchased_at",
        "timestamp",
        "date",
        "returned_at",
        "return_date",
        "updated_at",
        "created_at",
    )
    ID_COLS = (
        "transaction_id",
        "receipt_id",
        "sale_id",
        "sale_line_id",
        "line_id",
        "restock_order_id",
        "offer_id",
        "return_id",
        "order_id",
        "id",
    )

    def __init__(self) -> None:
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        self.supabase_key = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_KEY")
            or ""
        ).strip()
        self._table_columns_cache: Dict[str, List[str]] = {}
        self._branch_cost_cache: Dict[int, Dict[str, Decimal]] = {}

    @property
    def is_connected(self) -> bool:
        return bool(
            self.supabase_url
            and self.supabase_key
            and "PASTE" not in self.supabase_key.upper()
            and "YOUR_PROJECT_ID" not in self.supabase_url
            and "YOUR_SERVICE_ROLE_KEY" not in self.supabase_key
        )

    def _rest_request(
        self,
        table: str,
        params: Sequence[Tuple[str, str]] | Dict[str, str],
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 15,
    ) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, str]]:
        """Executes a PostgREST GET request and returns (rows_or_None, lowercase_response_headers)."""
        if not self.is_connected:
            return None, {}
        query = urllib.parse.urlencode(list(params.items()) if isinstance(params, dict) else list(params))
        url = f"{self.supabase_url}/rest/v1/{table}?{query}"
        headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                raw_body = resp.read().decode("utf-8").strip()
                data = json.loads(raw_body) if raw_body else []
                return (data if isinstance(data, list) else None), resp_headers
        except Exception as exc:
            print(f"[Supabase] Table '{table}' query skipped (using fallback): {exc}")
            return None, {}

    def _rest_select(
        self,
        table: str,
        params: Sequence[Tuple[str, str]] | Dict[str, str],
    ) -> Optional[List[Dict[str, Any]]]:
        rows, _ = self._rest_request(table, params)
        return rows

    def _rest_count(
        self,
        table: str,
        params: Sequence[Tuple[str, str]] | Dict[str, str],
    ) -> Optional[int]:
        """Gets the EXACT total row count matching `params` via PostgREST `Prefer: count=exact`."""
        param_list = list(params.items()) if isinstance(params, dict) else list(params)
        if not any(k == "select" for k, _ in param_list):
            param_list.append(("select", "*"))
        _, headers = self._rest_request(
            table,
            param_list,
            extra_headers={"Prefer": "count=exact", "Range": "0-0"},
        )
        content_range = headers.get("content-range", "")
        # Example: "0-0/124180" or "*/0"
        m = re.search(r"/(\d+)$", content_range)
        return int(m.group(1)) if m else None

    def _rest_select_paginated(
        self,
        table: str,
        params: Sequence[Tuple[str, str]] | Dict[str, str],
        *,
        page_size: int = 1000,
        max_rows: int = 60000,
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Fetches ALL rows across PostgREST 1,000-row pages up to `max_rows`, plus exact total count."""
        base_params = list(params.items()) if isinstance(params, dict) else list(params)
        all_rows: List[Dict[str, Any]] = []
        exact_total: Optional[int] = None
        offset = 0

        while offset < max_rows:
            end = min(offset + page_size - 1, max_rows - 1)
            hdrs = {"Range": f"{offset}-{end}", "Range-Unit": "items"}
            if offset == 0:
                hdrs["Prefer"] = "count=exact"
            batch, resp_headers = self._rest_request(table, base_params, extra_headers=hdrs, timeout=20)
            if offset == 0:
                m = re.search(r"/(\d+)$", resp_headers.get("content-range", ""))
                if m:
                    exact_total = int(m.group(1))
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        return all_rows, exact_total

    def _discover_columns(self, table: str) -> List[str]:
        """Fetches 1 row to inspect real column names so server-side queries never fail."""
        if table in self._table_columns_cache:
            return self._table_columns_cache[table]
        sample = self._rest_select(table, {"select": "*", "limit": "1"})
        cols = list(sample[0].keys()) if sample else []
        self._table_columns_cache[table] = cols
        if cols:
            print(f"[Supabase] Discovered '{table}' columns: {cols}")
        return cols

    def _find_date_column(self, table: str) -> Optional[str]:
        """Finds the best business date/timestamp column, prioritizing non-upload dates over created_at."""
        cols = self._discover_columns(table)
        if not cols:
            return None
        # 1. Known business date columns (excluding created_at / updated_at first)
        for c in self.BUSINESS_TIME_COLS:
            if c in cols and c not in ("created_at", "updated_at"):
                return c
        # 2. Any other column whose name hints at a business date/timestamp
        for c in cols:
            lc = c.lower()
            if lc in ("created_at", "updated_at", "synced_at", "uploaded_at"):
                continue
            if any(tok in lc for tok in ("date", "time", "sold", "occur", "_at", "_ts")):
                return c
        # 3. Last resort: created_at / updated_at
        for c in ("updated_at", "created_at"):
            if c in cols:
                return c
        return None

    def _build_desc_order_clause(self, table: str) -> Optional[str]:
        cols = self._discover_columns(table)
        if not cols:
            return None
        order_parts: List[str] = []
        date_col = self._find_date_column(table)
        if date_col:
            order_parts.append(f"{date_col}.desc")
        for c in self.ID_COLS:
            if c in cols and f"{c}.desc" not in order_parts:
                order_parts.append(f"{c}.desc")
                break
        return ",".join(order_parts) if order_parts else None

    @staticmethod
    def _parse_iso_date(val: Any) -> Optional[date]:
        if val is None:
            return None
        s = str(val).strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            return None
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    def _extract_business_timestamp(self, row: Dict[str, Any], preferred_col: Optional[str] = None) -> str:
        if preferred_col and row.get(preferred_col) is not None and str(row.get(preferred_col)).strip():
            return str(row[preferred_col]).strip()
        for k in self.BUSINESS_TIME_COLS:
            val = row.get(k)
            if val is not None and str(val).strip():
                return str(val).strip()
        return "N/A"

    def _row_sort_key(self, row: Dict[str, Any], preferred_date_col: Optional[str] = None) -> Tuple[str, int]:
        ts = self._extract_business_timestamp(row, preferred_col=preferred_date_col)
        if ts == "N/A":
            ts = ""
        num_id = 0
        for k in self.ID_COLS:
            val = row.get(k)
            if val is not None:
                digits = re.findall(r"\d+", str(val))
                if digits:
                    num_id = int(digits[-1])
                    break
        return (ts, num_id)

    def _get_date_bounds(self, table: str, branch_id: int, date_col: Optional[str]) -> Tuple[Optional[date], Optional[date]]:
        """Returns (min_date, max_date) present for `branch_id` in `table` using 2 lightweight limit=1 queries."""
        if not date_col:
            return None, None
        min_rows = self._rest_select(
            table,
            {"branch_id": f"eq.{branch_id}", "select": date_col, "order": f"{date_col}.asc", "limit": "1"},
        ) or []
        max_rows = self._rest_select(
            table,
            {"branch_id": f"eq.{branch_id}", "select": date_col, "order": f"{date_col}.desc", "limit": "1"},
        ) or []
        min_d = self._parse_iso_date(min_rows[0].get(date_col)) if min_rows else None
        max_d = self._parse_iso_date(max_rows[0].get(date_col)) if max_rows else None
        return min_d, max_d

    def _get_branch_cost_map(self, branch_id: int) -> Dict[str, Decimal]:
        """Loads and caches real `product_id -> cost_price` from Supabase `products` for true COGS."""
        if branch_id in self._branch_cost_cache:
            return self._branch_cost_cache[branch_id]

        cost_map: Dict[str, Decimal] = {}
        for pid, item in PRODUCTS_BY_ID.items():
            if item.get("cost_price") is not None:
                cost_map[str(pid)] = _money(item["cost_price"])

        prod_cols = self._discover_columns("products")
        if prod_cols:
            pk_col = "product_id" if "product_id" in prod_cols else ("id" if "id" in prod_cols else None)
            cost_col = "cost_price" if "cost_price" in prod_cols else ("cost" if "cost" in prod_cols else None)
            if pk_col and cost_col:
                params: List[Tuple[str, str]] = [("select", f"{pk_col},{cost_col}")]
                if "branch_id" in prod_cols:
                    params.append(("branch_id", f"eq.{branch_id}"))
                rows, _ = self._rest_select_paginated("products", params, page_size=1000, max_rows=10000)
                for p in rows:
                    pid = p.get(pk_col)
                    cval = p.get(cost_col)
                    if pid is not None and cval is not None:
                        cost_map[str(pid)] = _money(cval)

        self._branch_cost_cache[branch_id] = cost_map
        return cost_map

    def _get_product_lookup(self, branch_id: int, product_ids: Optional[Sequence[Any]] = None) -> Dict[str, Dict[str, Any]]:
        """Resolves real product names/SKUs from Supabase `products` for the exact product_ids needed."""
        lookup: Dict[str, Dict[str, Any]] = {}
        for pid, item in PRODUCTS_BY_ID.items():
            lookup[str(pid)] = dict(item)

        prod_cols = self._discover_columns("products")
        pk_col = "product_id" if "product_id" in prod_cols else ("id" if "id" in prod_cols else None)
        has_branch = "branch_id" in prod_cols

        if pk_col and product_ids:
            unique_ids = sorted({str(p).strip() for p in product_ids if p is not None and str(p).strip()})
            if unique_ids:
                params: Dict[str, str] = {
                    pk_col: f"in.({','.join(unique_ids[:100])})",
                    "select": "*",
                    "limit": "200",
                }
                if has_branch:
                    params["branch_id"] = f"eq.{branch_id}"
                rows = self._rest_select("products", params) or []
                for p in rows:
                    pid = p.get(pk_col)
                    if pid is not None:
                        lookup[str(pid)] = dict(p)
                if rows:
                    return lookup

        params = {"select": "*", "limit": "500"}
        if has_branch:
            params["branch_id"] = f"eq.{branch_id}"
        prod_rows = self._rest_select("products", params) or []
        for p in prod_rows:
            pid = p.get("product_id") or p.get("id")
            if pid is not None:
                lookup[str(pid)] = dict(p)
        return lookup

    def get_recent_receipts(self, branch_id: int, limit_receipts: int = 10) -> List[Dict[str, Any]]:
        """Fetches the TRUE latest receipts from Supabase `sales_lines` using server-side ordering."""
        sales_cols = self._discover_columns("sales_lines")
        date_col = self._find_date_column("sales_lines")
        order_clause = self._build_desc_order_clause("sales_lines")
        params: Dict[str, str] = {
            "branch_id": f"eq.{branch_id}",
            "select": "*",
            "limit": "500",
        }
        if order_clause:
            params["order"] = order_clause

        sales_rows = self._rest_select("sales_lines", params)
        if not sales_rows:
            return []

        tx_col = next(
            (c for c in ("transaction_id", "receipt_id", "sale_id", "order_id", "id", "sale_line_id") if c in sales_cols),
            None,
        )

        if tx_col and tx_col in ("transaction_id", "receipt_id", "sale_id", "order_id"):
            seen_tx: List[str] = []
            for r in sales_rows:
                val = r.get(tx_col)
                if val is not None:
                    s_val = str(val).strip()
                    if s_val and s_val not in seen_tx:
                        seen_tx.append(s_val)
                        if len(seen_tx) >= limit_receipts:
                            break
            if seen_tx:
                in_vals = ",".join(
                    v if re.fullmatch(r"\d+", v) else f'"{v}"' for v in seen_tx
                )
                complete_rows = self._rest_select(
                    "sales_lines",
                    {
                        "branch_id": f"eq.{branch_id}",
                        tx_col: f"in.({in_vals})",
                        "select": "*",
                        "limit": "500",
                    },
                )
                if complete_rows:
                    sales_rows = complete_rows

        needed_pids = [r.get("product_id") for r in sales_rows if r.get("product_id") is not None]
        product_lookup = self._get_product_lookup(branch_id, product_ids=needed_pids)

        grouped: Dict[str, Dict[str, Any]] = {}
        for row in sales_rows:
            tx_key = str(
                (row.get(tx_col) if tx_col else None)
                or row.get("transaction_id")
                or row.get("receipt_id")
                or row.get("sale_id")
                or row.get("order_id")
                or row.get("id")
                or row.get("sale_line_id")
                or "unknown"
            )
            sort_key = self._row_sort_key(row, preferred_date_col=date_col)
            pid = row.get("product_id")
            p_info = product_lookup.get(str(pid), {}) if pid is not None else {}
            prod_name = (
                row.get("product_name")
                or p_info.get("product_name")
                or p_info.get("name")
                or (f"Product #{pid}" if pid is not None else "Unknown Item")
            )
            sku = row.get("sku") or p_info.get("sku") or (f"SKU-{int(pid):04d}" if str(pid).isdigit() else "")
            qty = int(row.get("quantity") or row.get("qty") or 1)
            unit_price = _money(
                row.get("unit_price")
                or row.get("price")
                or p_info.get("unit_price")
                or p_info.get("price")
                or 0
            )
            disc = _money(row.get("discount_amount") or 0)
            line_gross = _money(row.get("line_total") or (unit_price * qty))
            line_net = _money(line_gross - disc)

            if tx_key not in grouped:
                grouped[tx_key] = {
                    "receipt_id": tx_key,
                    "timestamp": sort_key[0] or "N/A",
                    "_sort_key": sort_key,
                    "items": [],
                    "total_units": 0,
                    "gross_total_egp": Decimal("0.00"),
                    "discount_total_egp": Decimal("0.00"),
                    "net_total_egp": Decimal("0.00"),
                }
            rec = grouped[tx_key]
            if sort_key > rec["_sort_key"]:
                rec["_sort_key"] = sort_key
                if sort_key[0]:
                    rec["timestamp"] = sort_key[0]

            rec["items"].append(
                {
                    "product_id": pid,
                    "product_name": prod_name,
                    "sku": sku,
                    "quantity": qty,
                    "unit_price_egp": str(unit_price),
                    "discount_egp": str(disc),
                    "line_total_egp": str(line_net),
                }
            )
            rec["total_units"] += qty
            rec["gross_total_egp"] += line_gross
            rec["discount_total_egp"] += disc
            rec["net_total_egp"] += line_net

        sorted_receipts = sorted(grouped.values(), key=lambda x: x["_sort_key"], reverse=True)
        out: List[Dict[str, Any]] = []
        for idx, rec in enumerate(sorted_receipts[:limit_receipts]):
            out.append(
                {
                    "rank_from_latest": idx + 1,
                    "receipt_id": rec["receipt_id"],
                    "timestamp": str(rec["timestamp"]).replace("T", " ")[:19],
                    "item_count": len(rec["items"]),
                    "total_units": rec["total_units"],
                    "gross_total_egp": f"{rec['gross_total_egp']:,.2f}",
                    "discount_total_egp": f"{rec['discount_total_egp']:,.2f}",
                    "net_total_egp": f"{rec['net_total_egp']:,.2f}",
                    "items": rec["items"][:20],
                }
            )
        return out

    @staticmethod
    def _month_bounds(yr: int, mo: int) -> Tuple[date, date]:
        start_d = date(yr, mo, 1)
        last_day = calendar.monthrange(yr, mo)[1]
        end_excl = date(yr, mo, last_day) + timedelta(days=1)
        return start_d, end_excl

    def _resolve_date_window(
        self,
        period_spec: Dict[str, Any],
        db_max_date: Optional[date],
    ) -> Tuple[Optional[date], Optional[date], str, str]:
        """Returns (start_date_inclusive, end_date_exclusive, report_title_prefix, period_label)."""
        today_d = datetime.now(timezone.utc).date()
        kind = period_spec.get("kind", "daily")

        if kind == "all_time":
            return None, None, "ALL-TIME BRANCH SALES & KPI REPORT", "All Available Dates"

        if kind == "month":
            yr, mo = int(period_spec["year"]), int(period_spec["month"])
            s, e = self._month_bounds(yr, mo)
            mo_name = s.strftime("%B %Y")
            return s, e, f"MONTHLY SALES & KPI REPORT ({mo_name.upper()})", f"{s.isoformat()} to {(e - timedelta(days=1)).isoformat()}"

        if kind == "last_month":
            # Previous calendar month relative to today (e.g., Nov 2026 -> Oct 2026)
            first_of_this = today_d.replace(day=1)
            last_of_prev = first_of_this - timedelta(days=1)
            s, e = self._month_bounds(last_of_prev.year, last_of_prev.month)
            mo_name = s.strftime("%B %Y")
            return s, e, f"LAST MONTH SALES & KPI REPORT ({mo_name.upper()})", f"{s.isoformat()} to {(e - timedelta(days=1)).isoformat()}"

        if kind == "this_month":
            s, e = self._month_bounds(today_d.year, today_d.month)
            mo_name = s.strftime("%B %Y")
            return s, e, f"CURRENT MONTH SALES & KPI REPORT ({mo_name.upper()})", f"{s.isoformat()} to {(e - timedelta(days=1)).isoformat()}"

        if kind == "last_7_days":
            ref = db_max_date if (db_max_date and db_max_date < today_d) else today_d
            s = ref - timedelta(days=6)
            e = ref + timedelta(days=1)
            return s, e, "WEEKLY (7-DAY) BRANCH SALES & KPI REPORT", f"{s.isoformat()} to {ref.isoformat()}"

        if kind == "last_30_days":
            ref = db_max_date if (db_max_date and db_max_date < today_d) else today_d
            s = ref - timedelta(days=29)
            e = ref + timedelta(days=1)
            return s, e, "30-DAY BRANCH SALES & KPI REPORT", f"{s.isoformat()} to {ref.isoformat()}"

        if kind == "yesterday":
            ref = (today_d - timedelta(days=1))
            if db_max_date and db_max_date < ref:
                ref = db_max_date - timedelta(days=1)
            return ref, ref + timedelta(days=1), "DAILY BRANCH SALES & KPI REPORT", f"{ref.isoformat()} (1 Day)"

        # Default: daily -> check today first; if dataset ends earlier, use latest active business day
        ref = today_d
        note = "Today"
        if db_max_date and db_max_date < today_d:
            ref = db_max_date
            note = "Latest Active Day in DB"
        return ref, ref + timedelta(days=1), "DAILY BRANCH SALES & KPI REPORT", f"{ref.isoformat()} ({note})"

    def get_sales_report_data(
        self,
        branch_id: int,
        period_spec: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Computes branch sales KPIs from ALL matching Supabase rows in the requested date window."""
        base = FALLBACK_BRANCH_META[branch_id]
        prod = PRODUCTS_BY_ID[branch_id]
        spec = period_spec or {"kind": "daily"}
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.is_connected:
            sales_cols = self._discover_columns("sales_lines")
            date_col = self._find_date_column("sales_lines")
            total_branch_rows = self._rest_count("sales_lines", [("branch_id", f"eq.{branch_id}")])
            db_min_date, db_max_date = self._get_date_bounds("sales_lines", branch_id, date_col)

            start_d, end_excl_d, report_title, period_label = self._resolve_date_window(spec, db_max_date)

            # Select only the lean columns needed for fast paginated aggregation
            wanted_cols = [
                c for c in (
                    "product_id", "quantity", "qty", "unit_price", "price",
                    "line_total", "discount_amount", "cost_price", "unit_cost",
                    "transaction_id", "receipt_id", "sale_id", "offer_id", "id",
                )
                if c in sales_cols
            ]
            if date_col and date_col not in wanted_cols:
                wanted_cols.append(date_col)
            select_expr = ",".join(wanted_cols) if wanted_cols else "*"

            def _fetch_window(s_d: Optional[date], e_d: Optional[date]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
                q_params: List[Tuple[str, str]] = [
                    ("branch_id", f"eq.{branch_id}"),
                    ("select", select_expr),
                ]
                if date_col and s_d is not None:
                    q_params.append((date_col, f"gte.{s_d.isoformat()}T00:00:00Z"))
                if date_col and e_d is not None:
                    q_params.append((date_col, f"lt.{e_d.isoformat()}T00:00:00Z"))
                order_clause = self._build_desc_order_clause("sales_lines")
                if order_clause:
                    q_params.append(("order", order_clause))
                return self._rest_select_paginated("sales_lines", q_params, page_size=1000, max_rows=60000)

            sales_rows, window_exact_count = _fetch_window(start_d, end_excl_d)

            # If 'this_month' has 0 rows (e.g., dataset ends last month), report 0 clearly instead of faking!
            # Only if total_branch_rows > 0 do we return live Supabase results (even if 0 for an empty month).
            if sales_rows or (total_branch_rows is not None and total_branch_rows > 0):
                cost_map = self._get_branch_cost_map(branch_id)
                gross_sales = Decimal("0.00")
                discount_total = Decimal("0.00")
                total_cost = Decimal("0.00")
                units_sold = 0
                tx_ids = set()
                offer_redemptions = 0
                real_cost_hits = 0

                for r in sales_rows:
                    qty = int(r.get("quantity") or r.get("qty") or 1)
                    unit_price = _money(r.get("unit_price") or r.get("price") or 0)
                    line_gross = _money(r.get("line_total") or (unit_price * qty))
                    disc = _money(r.get("discount_amount") or 0)

                    # Priority: row cost_price -> live Supabase products.cost_price -> 70% estimate
                    raw_cost = r.get("cost_price") if r.get("cost_price") is not None else r.get("unit_cost")
                    if raw_cost is not None:
                        cost_u = _money(raw_cost)
                        real_cost_hits += 1
                    elif r.get("product_id") is not None and str(r.get("product_id")) in cost_map:
                        cost_u = cost_map[str(r.get("product_id"))]
                        real_cost_hits += 1
                    else:
                        cost_u = _money(unit_price * Decimal("0.70"))

                    gross_sales += line_gross
                    discount_total += disc
                    total_cost += _money(cost_u * qty)
                    units_sold += qty
                    tx_id = r.get("transaction_id") or r.get("receipt_id") or r.get("sale_id") or r.get("id")
                    if tx_id is not None:
                        tx_ids.add(tx_id)
                    if r.get("offer_id") or disc > 0:
                        offer_redemptions += 1

                # Fetch returns within the exact same date window
                ret_date_col = self._find_date_column("returns")
                ret_params: List[Tuple[str, str]] = [("branch_id", f"eq.{branch_id}"), ("select", "*")]
                if ret_date_col and start_d is not None:
                    ret_params.append((ret_date_col, f"gte.{start_d.isoformat()}T00:00:00Z"))
                if ret_date_col and end_excl_d is not None:
                    ret_params.append((ret_date_col, f"lt.{end_excl_d.isoformat()}T00:00:00Z"))
                returns_rows, _ = self._rest_select_paginated("returns", ret_params, page_size=1000, max_rows=10000)
                returns_total = sum(
                    (_money(x.get("refund_amount") or x.get("amount") or 0) for x in returns_rows),
                    Decimal("0.00"),
                )

                net_revenue = _money(gross_sales - discount_total - returns_total)
                gross_profit = _money(net_revenue - total_cost)
                margin_pct = (
                    ((gross_profit / net_revenue) * Decimal("100")).quantize(Decimal("0.1"))
                    if net_revenue > 0
                    else Decimal("0.0")
                )
                tx_count = len(tx_ids) if sales_rows else 0
                atv = _money(net_revenue / Decimal(tx_count)) if tx_count > 0 else Decimal("0.00")

                matched_total = window_exact_count if window_exact_count is not None else len(sales_rows)
                branch_total_str = f"{total_branch_rows:,}" if total_branch_rows is not None else f"{len(sales_rows):,}"
                truncated_note = f" (capped at {len(sales_rows):,})" if matched_total > len(sales_rows) else ""
                db_range_str = (
                    f"{db_min_date.isoformat()} to {db_max_date.isoformat()}"
                    if (db_min_date and db_max_date)
                    else "N/A"
                )
                cogs_note = (
                    "Verified Products Catalog Cost"
                    if (sales_rows and real_cost_hits >= len(sales_rows) * 0.5)
                    else "Estimated 70% COGS Fallback"
                )

                return {
                    "branch_id": branch_id,
                    "branch_name": base["name"],
                    "branch_code": base["code"],
                    "city": base["city"],
                    "report_title": report_title,
                    "period_kind": spec.get("kind", "daily"),
                    "period_label": period_label,
                    "report_date": period_label,
                    "date_column_used": date_col or "none",
                    "db_date_range": db_range_str,
                    "rows_analyzed": len(sales_rows),
                    "period_matching_rows": matched_total,
                    "total_branch_rows": total_branch_rows or len(sales_rows),
                    "data_source": (
                        f"Live Supabase DB ({len(sales_rows):,} rows in period{truncated_note} | "
                        f"{branch_total_str} total branch rows)"
                    ),
                    "cogs_source": cogs_note,
                    "gross_sales": f"{gross_sales:,.2f}",
                    "returns_total": f"{returns_total:,.2f}",
                    "net_revenue": f"{net_revenue:,.2f}",
                    "gross_profit": f"{gross_profit:,.2f}",
                    "margin_pct": str(margin_pct),
                    "transactions": tx_count,
                    "units_sold": units_sold,
                    "atv": f"{atv:,.2f}",
                    "offer_redemptions": offer_redemptions,
                    "offer_discount_total": f"{discount_total:,.2f}",
                    "flagged_product": prod["product_name"],
                    "days_until_expiry": prod["days_until_expiry"],
                    "report_recipient": base["report_recipient"],
                }

        return {
            "branch_id": branch_id,
            "branch_name": base["name"],
            "branch_code": base["code"],
            "city": base["city"],
            "report_title": "DAILY BRANCH SALES & KPI REPORT",
            "period_kind": spec.get("kind", "daily"),
            "period_label": today_str,
            "report_date": today_str,
            "date_column_used": "pilot_seed",
            "db_date_range": today_str,
            "rows_analyzed": base["transactions"],
            "period_matching_rows": base["transactions"],
            "total_branch_rows": base["transactions"],
            "data_source": "Pilot Branch Seed Data (Supabase sales_lines empty or unset)",
            "cogs_source": "Pilot Seed Cost",
            "gross_sales": f"{base['gross_sales']:,.2f}",
            "returns_total": "0.00",
            "net_revenue": f"{base['net_revenue']:,.2f}",
            "gross_profit": f"{base['gross_profit']:,.2f}",
            "margin_pct": str(base["margin_pct"]),
            "transactions": base["transactions"],
            "units_sold": base["units_sold"],
            "atv": f"{base['atv']:,.2f}",
            "offer_redemptions": base["offer_redemptions"],
            "offer_discount_total": f"{base['offer_discount_total']:,.2f}",
            "flagged_product": prod["product_name"],
            "days_until_expiry": prod["days_until_expiry"],
            "report_recipient": base["report_recipient"],
        }

    def get_rich_branch_context(
        self,
        branch_id: int,
        *,
        harness: Optional[DuplicateSchedulerAndStaleDataHarness] = None,
        receipt_limit: int = 10,
        period_spec: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Builds a complete, branch-isolated live snapshot for free-text AI Q&A & PDF export."""
        sales_summary = self.get_sales_report_data(branch_id, period_spec=period_spec)
        active_offer = self.get_active_offers_data(branch_id)
        recent_receipts = self.get_recent_receipts(branch_id, limit_receipts=receipt_limit)

        restock_order = self._build_desc_order_clause("restock_orders")
        r_params: Dict[str, str] = {"branch_id": f"eq.{branch_id}", "select": "*", "limit": "15"}
        if restock_order:
            r_params["order"] = restock_order
        db_restock_rows = self._rest_select("restock_orders", r_params) or []

        session_restock_orders: List[Dict[str, Any]] = []
        recent_audit: List[Dict[str, Any]] = []
        if harness is not None:
            for order in harness.restock_workflow.saved_orders.values():
                if int(order.get("branch_id", branch_id)) == branch_id:
                    session_restock_orders.append(
                        {
                            "source": "Current Bot Session",
                            "restock_order_id": order.get("restock_order_id"),
                            "product_name": order.get("product_name"),
                            "sku": order.get("sku"),
                            "requested_quantity": order.get("requested_quantity"),
                            "supplier_name": order.get("supplier_name"),
                            "version": order.get("version"),
                            "status": order.get("status"),
                            "reviewer_feedback": order.get("user_feedback"),
                        }
                    )
            for rec in harness.audit.list_entries(branch_id=branch_id)[-8:]:
                recent_audit.append(
                    {
                        "time": str(rec.timestamp),
                        "action": rec.action,
                        "actor": rec.actor,
                        "version": rec.version,
                        "details": rec.details,
                    }
                )

        ret_order = self._build_desc_order_clause("returns")
        ret_params: Dict[str, str] = {"branch_id": f"eq.{branch_id}", "select": "*", "limit": "10"}
        if ret_order:
            ret_params["order"] = ret_order
        returns_rows = self._rest_select("returns", ret_params) or []

        prod_cols = self._discover_columns("products")
        p_params: Dict[str, str] = {"select": "*", "limit": "25"}
        if "branch_id" in prod_cols:
            p_params["branch_id"] = f"eq.{branch_id}"
        prod_rows = self._rest_select("products", p_params) or []

        return {
            "active_branch": {
                "branch_id": branch_id,
                "name": FALLBACK_BRANCH_META[branch_id]["name"],
                "code": FALLBACK_BRANCH_META[branch_id]["code"],
                "city": FALLBACK_BRANCH_META[branch_id]["city"],
                "manager_email": FALLBACK_BRANCH_META[branch_id]["approver"],
            },
            "sales_report_summary": sales_summary,
            "last_customer_receipt": recent_receipts[0] if recent_receipts else None,
            "recent_customer_receipts": recent_receipts,
            "recent_restock_purchase_orders": {
                "in_session_orders": session_restock_orders,
                "database_restock_orders": db_restock_rows[:10],
            },
            "active_offer": active_offer,
            "flagged_inventory_item": PRODUCTS_BY_ID[branch_id],
            "sample_branch_products": prod_rows[:15],
            "recent_returns": returns_rows[:10],
            "recent_audit_events": recent_audit,
        }

    def get_active_offers_data(self, branch_id: int) -> Dict[str, Any]:
        base = FALLBACK_BRANCH_META[branch_id]
        prod = PRODUCTS_BY_ID[branch_id]
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        offer_rows = self._rest_select(
            "offers",
            {"branch_id": f"eq.{branch_id}", "select": "*", "limit": "10"},
        )
        if offer_rows:
            offer_rows.sort(key=self._row_sort_key, reverse=True)
            row = offer_rows[0]
            reg_price = _money(row.get("regular_price") or prod["unit_price"])
            off_price = _money(row.get("offer_price") or base["offer_price"])
            cost_price = _money(row.get("cost_price") or prod["cost_price"])
            disc_pct = (
                ((reg_price - off_price) / reg_price * Decimal("100")).quantize(Decimal("0.1"))
                if reg_price > 0
                else base["discount_pct"]
            )
            live_margin = (
                ((off_price - cost_price) / off_price * Decimal("100")).quantize(Decimal("0.1"))
                if off_price > 0
                else Decimal("0.0")
            )
            return {
                "branch_id": branch_id,
                "branch_name": base["name"],
                "branch_code": base["code"],
                "city": base["city"],
                "report_date": today_str,
                "data_source": f"Live Supabase DB ({len(offer_rows)} offer record(s))",
                "offer_id": row.get("offer_id") or row.get("id") or base["offer_id"],
                "offer_title": row.get("title") or row.get("offer_type") or base["offer_title"],
                "version": row.get("version") or 1,
                "status": str(row.get("status") or "ACTIVE").upper(),
                "product_name": row.get("product_name") or prod["product_name"],
                "sku": row.get("sku") or prod["sku"],
                "regular_price": f"{reg_price:,.2f}",
                "offer_price": f"{off_price:,.2f}",
                "cost_price": f"{cost_price:,.2f}",
                "discount_pct": str(disc_pct),
                "live_margin_pct": str(live_margin),
                "min_margin_pct": "10.0",
                "stock_quantity": prod["stock_quantity"],
                "days_until_expiry": prod["days_until_expiry"],
            }

        reg_price = _money(prod["unit_price"])
        off_price = _money(base["offer_price"])
        cost_price = _money(prod["cost_price"])
        live_margin = ((off_price - cost_price) / off_price * Decimal("100")).quantize(Decimal("0.1"))
        return {
            "branch_id": branch_id,
            "branch_name": base["name"],
            "branch_code": base["code"],
            "city": base["city"],
            "report_date": today_str,
            "data_source": "Pilot Branch Seed Data (Supabase offers empty or unset)",
            "offer_id": base["offer_id"],
            "offer_title": base["offer_title"],
            "version": 1,
            "status": "ACTIVE",
            "product_name": prod["product_name"],
            "sku": prod["sku"],
            "regular_price": f"{reg_price:,.2f}",
            "offer_price": f"{off_price:,.2f}",
            "cost_price": f"{cost_price:,.2f}",
            "discount_pct": str(base["discount_pct"]),
            "live_margin_pct": str(live_margin),
            "min_margin_pct": "10.0",
            "stock_quantity": prod["stock_quantity"],
            "days_until_expiry": prod["days_until_expiry"],
        }


# ---------------------------------------------------------------------------
# 3. Zero-Dependency Pure-Python Executive PDF Generator (A4 PDF 1.4 + Multi-Column Tables)
# ---------------------------------------------------------------------------
class ExecutivePDFBuilder:
    """Generates clean, multi-page A4 executive PDFs with KPI cards, multi-column tables, and AI insights."""

    PAGE_W = 595.0
    PAGE_H = 842.0
    MARGIN_X = 42.0
    CONTENT_W = PAGE_W - 2 * MARGIN_X

    def __init__(self, title: str, subtitle: str, badge_text: str) -> None:
        self.title = self._clean(title)
        self.subtitle = self._clean(subtitle)
        self.badge_text = self._clean(badge_text)
        self.pages: List[List[str]] = []
        self.y = self.PAGE_H
        self._start_new_page(is_first=True)

    @staticmethod
    def _clean(text: str) -> str:
        replacements = {
            "\u2014": "-",
            "\u2013": "-",
            "\u2018": "'",
            "\u2019": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u2022": "*",
            "\u2794": "->",
            "\u2192": "->",
            "\u2501": "-",
            "\u00a0": " ",
        }
        s = strip_markdown_formatting(str(text))
        for k, v in replacements.items():
            s = s.replace(k, v)
        s = s.encode("latin-1", "ignore").decode("latin-1")
        return re.sub(r"[ \t]+", " ", s).strip()

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def _cmd(self, s: str) -> None:
        self.pages[-1].append(s)

    def _rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill_rgb: Optional[Tuple[float, float, float]] = None,
        stroke_rgb: Optional[Tuple[float, float, float]] = None,
        lw: float = 0.7,
    ) -> None:
        ops = ["q"]
        if fill_rgb:
            ops.append(f"{fill_rgb[0]:.3f} {fill_rgb[1]:.3f} {fill_rgb[2]:.3f} rg")
        if stroke_rgb:
            ops.append(f"{stroke_rgb[0]:.3f} {stroke_rgb[1]:.3f} {stroke_rgb[2]:.3f} RG")
            ops.append(f"{lw:.2f} w")
        ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re")
        if fill_rgb and stroke_rgb:
            ops.append("B")
        elif fill_rgb:
            ops.append("f")
        elif stroke_rgb:
            ops.append("S")
        ops.append("Q")
        self._cmd(" ".join(ops))

    def _text(
        self,
        x: float,
        y: float,
        text: str,
        font: str = "F1",
        size: float = 10.0,
        rgb: Tuple[float, float, float] = (0.12, 0.16, 0.22),
    ) -> None:
        clean = self._esc(self._clean(text))
        self._cmd(
            f"BT /{font} {size:.1f} Tf {rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm ({clean}) Tj ET"
        )

    def _wrap_lines(self, text: str, max_chars: int) -> List[str]:
        out: List[str] = []
        for raw_para in str(text).splitlines():
            para = self._clean(raw_para)
            if not para:
                out.append("")
                continue
            words = para.split(" ")
            cur = ""
            for w in words:
                candidate = f"{cur} {w}".strip()
                if len(candidate) <= max_chars:
                    cur = candidate
                else:
                    if cur:
                        out.append(cur)
                    while len(w) > max_chars:
                        out.append(w[:max_chars])
                        w = w[max_chars:]
                    cur = w
            if cur:
                out.append(cur)
        return out or [""]

    def _start_new_page(self, is_first: bool = False) -> None:
        self.pages.append([])
        if is_first:
            banner_h = 84.0
            banner_y = self.PAGE_H - banner_h
            self._rect(0, banner_y, self.PAGE_W, banner_h, fill_rgb=(0.06, 0.24, 0.22))
            self._rect(0, banner_y, self.PAGE_W, 4.0, fill_rgb=(0.13, 0.65, 0.52))
            self._text(self.MARGIN_X, self.PAGE_H - 34, self.title, font="F2", size=16.5, rgb=(1, 1, 1))
            self._text(self.MARGIN_X, self.PAGE_H - 54, self.subtitle, font="F1", size=9.6, rgb=(0.82, 0.93, 0.90))
            self._text(self.MARGIN_X, self.PAGE_H - 70, self.badge_text, font="F2", size=8.2, rgb=(0.55, 0.95, 0.80))
            self.y = banner_y - 22.0
        else:
            self._rect(0, self.PAGE_H - 34, self.PAGE_W, 34, fill_rgb=(0.06, 0.24, 0.22))
            self._text(self.MARGIN_X, self.PAGE_H - 22, f"{self.title} (Continued)", font="F2", size=10.5, rgb=(1, 1, 1))
            self.y = self.PAGE_H - 54.0

    def _ensure_space(self, needed: float) -> None:
        if self.y - needed < 56.0:
            self._start_new_page(is_first=False)

    def add_section_header(self, title: str, min_following_space: float = 24.0) -> None:
        self._ensure_space(34.0 + min_following_space)
        self.y -= 6.0
        self._text(self.MARGIN_X, self.y, title.upper(), font="F2", size=10.6, rgb=(0.06, 0.28, 0.25))
        self.y -= 6.0
        self._rect(self.MARGIN_X, self.y, self.CONTENT_W, 1.2, fill_rgb=(0.13, 0.65, 0.52))
        self.y -= 14.0

    def add_kpi_cards(self, cards: List[Tuple[str, str, str]]) -> None:
        self._ensure_space(68.0)
        gap = 10.0
        cols = min(3, max(1, len(cards)))
        card_w = (self.CONTENT_W - gap * (cols - 1)) / cols
        card_h = 56.0
        top_y = self.y

        for idx, (label, value, subtext) in enumerate(cards[:3]):
            cx = self.MARGIN_X + idx * (card_w + gap)
            cy = top_y - card_h
            self._rect(cx, cy, card_w, card_h, fill_rgb=(0.95, 0.98, 0.97), stroke_rgb=(0.78, 0.88, 0.85), lw=0.8)
            self._rect(cx, top_y - 3.5, card_w, 3.5, fill_rgb=(0.13, 0.65, 0.52))
            self._text(cx + 10, top_y - 18, label.upper(), font="F2", size=7.8, rgb=(0.35, 0.45, 0.43))
            self._text(cx + 10, top_y - 36, value, font="F2", size=12.8, rgb=(0.06, 0.22, 0.20))
            if subtext:
                self._text(cx + 10, top_y - 49, subtext, font="F1", size=7.8, rgb=(0.35, 0.42, 0.45))

        self.y = top_y - card_h - 14.0

    def add_kv_table(self, rows: List[Tuple[str, str]]) -> None:
        row_h = 20.0
        col1_w = 185.0
        for idx, (k, v) in enumerate(rows):
            wrapped_v = self._wrap_lines(v, max_chars=54)
            h = max(row_h, 12.0 + len(wrapped_v) * 12.0)
            self._ensure_space(h + 4.0)
            ry = self.y - h
            bg = (0.97, 0.98, 0.99) if idx % 2 == 0 else (1.0, 1.0, 1.0)
            self._rect(self.MARGIN_X, ry, self.CONTENT_W, h, fill_rgb=bg, stroke_rgb=(0.88, 0.90, 0.92), lw=0.4)
            self._text(self.MARGIN_X + 8, self.y - 14, k, font="F2", size=9.0, rgb=(0.20, 0.26, 0.32))
            for li, line in enumerate(wrapped_v):
                self._text(self.MARGIN_X + col1_w, self.y - 14 - li * 12.0, line, font="F1", size=9.0, rgb=(0.10, 0.14, 0.18))
            self.y = ry
        self.y -= 12.0

    def add_data_table(
        self,
        headers: List[str],
        rows: List[List[str]],
        col_widths: List[float],
    ) -> None:
        """Draws a clean multi-column table with a dark header and alternating row striping."""
        total_w = sum(col_widths)
        scale = self.CONTENT_W / total_w if total_w > 0 else 1.0
        widths = [w * scale for w in col_widths]

        def draw_header() -> None:
            # Ensure enough room for header (22pt) + at least one row (34pt) so headers never orphan!
            self._ensure_space(58.0)
            hy = self.y - 22.0
            self._rect(self.MARGIN_X, hy, self.CONTENT_W, 22.0, fill_rgb=(0.08, 0.29, 0.26))
            cx = self.MARGIN_X
            for hdr, w in zip(headers, widths):
                self._text(cx + 6, self.y - 14.5, hdr, font="F2", size=8.4, rgb=(1, 1, 1))
                cx += w
            self.y = hy

        draw_header()
        for r_idx, row in enumerate(rows):
            wrapped_cells: List[List[str]] = []
            max_lines = 1
            for cell, w in zip(row, widths):
                max_chars = max(8, int((w - 10) / 4.8))
                lines = self._wrap_lines(str(cell), max_chars=max_chars)
                wrapped_cells.append(lines)
                max_lines = max(max_lines, len(lines))

            row_h = max(19.0, 9.0 + max_lines * 11.0)
            if self.y - row_h < 56.0:
                self._start_new_page(is_first=False)
                draw_header()

            ry = self.y - row_h
            bg = (0.97, 0.98, 0.99) if r_idx % 2 == 0 else (1.0, 1.0, 1.0)
            self._rect(self.MARGIN_X, ry, self.CONTENT_W, row_h, fill_rgb=bg, stroke_rgb=(0.88, 0.90, 0.92), lw=0.4)
            cx = self.MARGIN_X
            for lines, w in zip(wrapped_cells, widths):
                for li, ln in enumerate(lines):
                    self._text(cx + 6, self.y - 13.0 - li * 11.0, ln, font="F1", size=8.3, rgb=(0.12, 0.16, 0.22))
                cx += w
            self.y = ry
        self.y -= 12.0

    def add_ai_insight_box(self, heading: str, body: str) -> None:
        lines = self._wrap_lines(body, max_chars=82)
        remaining = list(lines)
        first_chunk = True
        while remaining:
            avail = self.y - 62.0
            max_lines = max(3, int((avail - 34.0) // 13.5))
            chunk = remaining[:max_lines]
            remaining = remaining[max_lines:]
            ch_h = (26.0 if first_chunk else 14.0) + len(chunk) * 13.5 + 8.0
            self._ensure_space(ch_h + 6.0)
            by = self.y - ch_h
            self._rect(self.MARGIN_X, by, self.CONTENT_W, ch_h, fill_rgb=(0.94, 0.97, 1.0), stroke_rgb=(0.76, 0.85, 0.96), lw=0.7)
            self._rect(self.MARGIN_X, by, 4.0, ch_h, fill_rgb=(0.12, 0.42, 0.78))
            ty = self.y - 16.0
            if first_chunk:
                self._text(self.MARGIN_X + 12, ty, heading, font="F2", size=9.6, rgb=(0.08, 0.28, 0.56))
                ty -= 16.0
                first_chunk = False
            for ln in chunk:
                if ln:
                    self._text(self.MARGIN_X + 12, ty, ln, font="F1", size=9.0, rgb=(0.12, 0.18, 0.26))
                ty -= 13.5
            self.y = by - 10.0
            if remaining:
                self._start_new_page(is_first=False)

    def build_bytes(self) -> bytes:
        total_pages = len(self.pages)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for idx, page_cmds in enumerate(self.pages, 1):
            page_cmds.append(f"q 0.85 0.88 0.90 rg {self.MARGIN_X:.2f} 38.00 {self.CONTENT_W:.2f} 0.80 re f Q")
            footer_l = self._esc(f"DEPI Retail AI Agents | Generated {ts} | Branch Isolated Report")
            footer_r = self._esc(f"Page {idx} of {total_pages}")
            page_cmds.append(f"BT /F1 8.0 Tf 0.45 0.50 0.55 rg 1 0 0 1 {self.MARGIN_X:.2f} 25.00 Tm ({footer_l}) Tj ET")
            page_cmds.append(f"BT /F1 8.0 Tf 0.45 0.50 0.55 rg 1 0 0 1 {self.PAGE_W - self.MARGIN_X - 48:.2f} 25.00 Tm ({footer_r}) Tj ET")

        objects: List[bytes] = []

        def add_obj(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        add_obj(b"<< /Type /Catalog /Pages 2 0 R >>")
        add_obj(b"")
        add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

        page_obj_ids: List[int] = []
        for cmds in self.pages:
            stream_data = "\n".join(cmds).encode("latin-1")
            compressed = zlib.compress(stream_data)
            content_id = add_obj(
                f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
                + compressed
                + b"\nendstream"
            )
            page_id = add_obj(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.PAGE_W:.0f} {self.PAGE_H:.0f}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>".encode("ascii")
            )
            page_obj_ids.append(page_id)

        kids = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
        objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_obj_ids)} >>".encode("ascii")

        buf = io.BytesIO()
        buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: List[int] = [0]
        for obj_num, body in enumerate(objects, 1):
            offsets.append(buf.tell())
            buf.write(f"{obj_num} 0 obj\n".encode("ascii"))
            buf.write(body)
            buf.write(b"\nendobj\n")

        xref_pos = buf.tell()
        buf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        buf.write(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            buf.write(f"{off:010d} 00000 n \n".encode("ascii"))

        buf.write(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
        )
        return buf.getvalue()


def generate_sales_report_pdf(report_data: Dict[str, Any], ai_summary: str, ai_badge: str) -> Path:
    GENERATED_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    title = report_data.get("report_title") or "DAILY BRANCH SALES & KPI REPORT"
    period_label = report_data.get("period_label") or report_data.get("report_date", "")
    period_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(report_data.get("period_kind", "daily"))).strip("_")
    ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    pdf = ExecutivePDFBuilder(
        title=title,
        subtitle=f"{report_data['branch_name']} ({report_data['branch_code']}) | Period: {period_label} (Africa/Cairo)",
        badge_text=f"ROWS: {report_data.get('rows_analyzed', 0):,} / {report_data.get('total_branch_rows', 0):,}  |  AI: {ai_badge}",
    )
    pdf.add_section_header("Executive KPI Snapshot")
    pdf.add_kpi_cards([
        ("Gross Sales", f"{report_data['gross_sales']} EGP", f"{report_data['transactions']:,} Transactions"),
        ("Net Revenue", f"{report_data['net_revenue']} EGP", f"{report_data['units_sold']:,} Units Sold"),
        ("Gross Profit", f"{report_data['gross_profit']} EGP", f"Margin: {report_data['margin_pct']}%"),
    ])
    pdf.add_section_header("Branch Performance & Database Coverage")
    pdf.add_kv_table([
        ("Branch Scope", f"{report_data['branch_name']} ({report_data['branch_code']} - {report_data['city']})"),
        ("Reporting Period", period_label),
        ("Database Coverage", report_data["data_source"]),
        ("DB Active Date Range", f"{report_data.get('db_date_range', 'N/A')} (column: {report_data.get('date_column_used', 'N/A')})"),
        ("COGS & Margin Basis", report_data.get("cogs_source", "Verified Products Catalog")),
        ("Average Transaction Value", f"{report_data['atv']} EGP"),
        ("Returns & Refunds", f"-{report_data.get('returns_total', '0.00')} EGP"),
        ("Offer Impact (H5)", f"{report_data['offer_redemptions']} redemptions (-{report_data['offer_discount_total']} EGP)"),
        ("Stock Alerts Flagged (H3)", f"1 item: {report_data['flagged_product']} ({report_data['days_until_expiry']}d to expiry)"),
        ("Authorized Report Recipient", report_data["report_recipient"]),
    ])
    pdf.add_section_header("AI Executive Analysis & Recommendations")
    pdf.add_ai_insight_box(f"AI Branch Analyst - {ai_badge}", ai_summary)

    filename = f"Sales_Report_Branch{report_data['branch_id']}_{report_data['branch_code']}_{period_slug}_{ts_slug}.pdf"
    out_path = GENERATED_REPORTS_DIR / filename
    out_path.write_bytes(pdf.build_bytes())
    return out_path


def generate_active_offers_pdf(offer_data: Dict[str, Any], ai_analysis: str, ai_badge: str) -> Path:
    GENERATED_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pdf = ExecutivePDFBuilder(
        title="ACTIVE PROMOTIONAL OFFERS & GUARDRAILS",
        subtitle=f"{offer_data['branch_name']} ({offer_data['branch_code']}) | Offer #{offer_data['offer_id']} (v{offer_data['version']})",
        badge_text=f"STATUS: {offer_data['status']}  |  GUARDRAILS: PASSED  |  AI: {ai_badge}",
    )
    pdf.add_section_header("Campaign Pricing & Margin Snapshot")
    pdf.add_kpi_cards([
        ("Promotional Price", f"{offer_data['offer_price']} EGP", f"Regular: {offer_data['regular_price']} EGP"),
        ("Discount Applied", f"{offer_data['discount_pct']}% OFF", f"Unit Cost: {offer_data['cost_price']} EGP"),
        ("Live Profit Margin", f"{offer_data['live_margin_pct']}%", f"Floor Min: {offer_data['min_margin_pct']}% (PASS)"),
    ])
    pdf.add_section_header("Offer Details & Cross-Workflow Controls")
    pdf.add_kv_table([
        ("Branch Scope", f"{offer_data['branch_name']} ({offer_data['branch_code']} - {offer_data['city']})"),
        ("Data Source", offer_data["data_source"]),
        ("Campaign Title", f"Offer #{offer_data['offer_id']} - {offer_data['offer_title']} (v{offer_data['version']})"),
        ("Target Product", f"{offer_data['product_name']} ({offer_data['sku']})"),
        ("Shelf Stock & Expiry", f"{offer_data['stock_quantity']} units on shelf | {offer_data['days_until_expiry']} day(s) left"),
        ("Price & Margin Guardrail", f"PASSED ({offer_data['live_margin_pct']}% >= {offer_data['min_margin_pct']}% min margin)"),
        ("Restocking Coordination (H2)", "Automated restock orders PAUSED while clearance offer is active"),
        ("POS Cashier Terminal", f"{offer_data['status']} & VERIFIED (Idempotent Activation)"),
    ])
    pdf.add_section_header("AI Offer Strategist Assessment")
    pdf.add_ai_insight_box(f"AI Offer Strategist - {ai_badge}", ai_analysis)

    filename = f"Active_Offers_Branch{offer_data['branch_id']}_{offer_data['branch_code']}_{offer_data['report_date']}.pdf"
    out_path = GENERATED_REPORTS_DIR / filename
    out_path.write_bytes(pdf.build_bytes())
    return out_path


def generate_db_query_pdf(
    question: str,
    ai_answer: str,
    branch_ctx: Dict[str, Any],
    ai_badge: str,
    *,
    original_question: Optional[str] = None,
    display_limit: Optional[int] = None,
) -> Path:
    """Generates a polished A4 Database Query PDF with the AI answer + structured live DB tables."""
    GENERATED_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    b = branch_ctx["active_branch"]
    sales = branch_ctx["sales_report_summary"]
    ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    pdf = ExecutivePDFBuilder(
        title="DATABASE QUERY & OPERATIONS REPORT",
        subtitle=f"{b['name']} ({b['code']} - {b['city']}) | Live Branch Query Export",
        badge_text=f"SOURCE: {sales['data_source'][:40]}  |  AI: {ai_badge}",
    )

    pdf.add_section_header("Query Request & Branch Scope")
    pdf.add_kv_table([
        ("Manager Request", question),
        ("Branch Scope", f"{b['name']} ({b['code']} - {b['city']})"),
        ("Data Source", sales["data_source"]),
    ])

    pdf.add_section_header("Retail AI Assistant Response")
    pdf.add_ai_insight_box(f"Retail AI Operations Answer ({ai_badge})", ai_answer)

    low_q = f"{question} {original_question or ''}".lower()
    receipts: List[Dict[str, Any]] = branch_ctx.get("recent_customer_receipts") or []
    products: List[Dict[str, Any]] = branch_ctx.get("sample_branch_products") or []
    returns: List[Dict[str, Any]] = branch_ctx.get("recent_returns") or []
    restock_all = (branch_ctx.get("recent_restock_purchase_orders") or {})
    restock_rows: List[Dict[str, Any]] = (
        (restock_all.get("in_session_orders") or [])
        + (restock_all.get("database_restock_orders") or [])
    )

    wants_returns = any(k in low_q for k in ("return", "refund", "مرتجع"))
    wants_products = any(k in low_q for k in ("product", "catalog", "sku", "stock", "inventory", "price", "منتج", "مخزون"))
    wants_restock = any(k in low_q for k in ("restock", "supplier", "purchase order", "po ", "توريد", "مورد"))
    wants_receipts = any(
        k in low_q
        for k in ("receipt", "order", "precede", "sale", "transaction", "invoice", "last", "recent", "فاتور", "فواتير", "ايصال", "إيصال")
    )

    if wants_returns and returns:
        pdf.add_section_header("Live Database Records: Recent Returns", min_following_space=60.0)
        rows_tbl = [
            [
                str(r.get("return_id") or r.get("id") or "-"),
                str(r.get("product_id") or "-"),
                str(r.get("quantity") or r.get("qty") or 1),
                f"{_money(r.get('refund_amount') or r.get('amount') or 0):,.2f} EGP",
                str(r.get("reason") or r.get("return_reason") or "Recorded return"),
            ]
            for r in returns[:10]
        ]
        pdf.add_data_table(["Return ID", "Product ID", "Qty", "Refund", "Reason"], rows_tbl, [75, 70, 45, 90, 230])

    if wants_restock and restock_rows:
        pdf.add_section_header("Live Database Records: Supplier Restock Purchase Orders", min_following_space=60.0)
        rows_tbl = [
            [
                str(r.get("restock_order_id") or r.get("id") or "-"),
                str(r.get("product_name") or f"Product #{r.get('product_id', '-')}"),
                str(r.get("requested_quantity") or r.get("quantity") or "-"),
                str(r.get("status") or "PENDING"),
                str(r.get("supplier_name") or f"Supplier #{r.get('supplier_id', '-')}"),
            ]
            for r in restock_rows[:10]
        ]
        pdf.add_data_table(["Order ID", "Product", "Qty", "Status", "Supplier"], rows_tbl, [65, 165, 45, 85, 150])

    if wants_products and products and not wants_receipts:
        pdf.add_section_header("Live Database Records: Branch Product Catalog", min_following_space=60.0)
        rows_tbl = [
            [
                str(p.get("sku") or f"ID-{p.get('product_id') or p.get('id')}"),
                str(p.get("product_name") or p.get("name") or "-"),
                str(p.get("category") or "-"),
                f"{_money(p.get('unit_price') or p.get('price') or 0):,.2f} EGP",
                f"{_money(p.get('cost_price') or p.get('cost') or 0):,.2f} EGP",
            ]
            for p in products[:15]
        ]
        pdf.add_data_table(["SKU", "Product Name", "Category", "Price", "Cost"], rows_tbl, [75, 185, 90, 80, 80])

    # Default or receipt/order queries -> show exact requested receipt count (or latest 10)
    if receipts and (wants_receipts or (not wants_returns and not wants_products and not wants_restock)):
        is_preceding = any(k in low_q for k in ("precede", "previous", "before it", "قبلها", "سابقة", "سابقه"))
        if is_preceding and display_limit and len(receipts) > 1:
            shown_receipts = receipts[1 : 1 + display_limit]
            tbl_title = f"Live Database Records: {len(shown_receipts)} Preceding Customer Receipts"
        else:
            max_show = display_limit if (display_limit and display_limit > 0) else 10
            shown_receipts = receipts[:max_show]
            tbl_title = f"Live Database Records: Recent Customer Receipts (Latest {len(shown_receipts)})"

        pdf.add_section_header(tbl_title, min_following_space=60.0)
        rows_tbl: List[List[str]] = []
        for rec in shown_receipts:
            clean_ts = str(rec["timestamp"]).replace("T", " ")[:19]
            for item in rec["items"]:
                rows_tbl.append(
                    [
                        f"#{rec['receipt_id']}",
                        clean_ts,
                        f"{item['product_name']} ({item['sku']})" if item.get("sku") else str(item["product_name"]),
                        str(item["quantity"]),
                        f"{item['unit_price_egp']} EGP",
                        f"{item['line_total_egp']} EGP",
                    ]
                )
        pdf.add_data_table(
            ["Receipt ID", "Timestamp", "Product", "Qty", "Unit Price", "Line Total"],
            rows_tbl,
            [64, 108, 166, 34, 69, 69],
        )

    filename = f"DB_Query_Branch{b['branch_id']}_{b['code']}_{ts_slug}.pdf"
    out_path = GENERATED_REPORTS_DIR / filename
    out_path.write_bytes(pdf.build_bytes())
    return out_path


# ---------------------------------------------------------------------------
# 4. Telegram HTTP Helpers (JSON Messages + Typing Action + Multipart PDF)
# ---------------------------------------------------------------------------
def telegram_api_call(
    bot_token: str,
    method: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token.strip()}/{method}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    else:
        req = urllib.request.Request(url, method="GET")

    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_typing_action(bot_token: str, chat_id: str) -> None:
    try:
        telegram_api_call(bot_token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


def build_multipart_document_body(
    chat_id: str,
    file_path: Path,
    caption: str = "",
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, str]:
    boundary = f"----RetailOpsBoundary{uuid.uuid4().hex}"
    parts: List[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )

    add_field("chat_id", str(chat_id))
    if caption:
        add_field("caption", caption[:1000])
    if reply_markup is not None:
        add_field("reply_markup", json.dumps(reply_markup))

    file_bytes = file_path.read_bytes()
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="{file_path.name}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_telegram_document(
    bot_token: str,
    chat_id: str,
    file_path: Path,
    caption: str = "",
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body, content_type = build_multipart_document_body(chat_id, file_path, caption, reply_markup)
    url = f"https://api.telegram.org/bot{bot_token.strip()}/sendDocument"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": strip_markdown_formatting(text)}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    telegram_api_call(bot_token, "sendMessage", payload)


def resolve_chat_id(
    bot_token: str,
    explicit_chat_id: Optional[str] = None,
    timeout_seconds: int = 120,
) -> str:
    me = telegram_api_call(bot_token, "getMe").get("result", {})
    bot_username = me.get("username", "Retail_ops_bot")
    print(f"[Telegram] Connected to bot: @{bot_username} (id={me.get('id')})")

    if explicit_chat_id and explicit_chat_id.strip():
        chat_id = explicit_chat_id.strip()
        CHAT_ID_CACHE_FILE.write_text(chat_id, encoding="utf-8")
        return chat_id

    updates = telegram_api_call(bot_token, "getUpdates").get("result", [])
    for upd in reversed(updates):
        msg = (
            upd.get("message")
            or upd.get("edited_message")
            or upd.get("callback_query", {}).get("message")
            or {}
        )
        chat = msg.get("chat", {})
        if "id" in chat:
            chat_id = str(chat["id"])
            CHAT_ID_CACHE_FILE.write_text(chat_id, encoding="utf-8")
            print(f"[Telegram] Auto-detected & cached chat_id={chat_id}")
            return chat_id

    if CHAT_ID_CACHE_FILE.exists():
        cached_id = CHAT_ID_CACHE_FILE.read_text(encoding="utf-8").strip()
        if cached_id:
            print(f"[Telegram] Loaded saved chat_id={cached_id}")
            return cached_id

    deadline = time.time() + timeout_seconds
    printed_prompt = False
    while time.time() < deadline:
        updates = telegram_api_call(bot_token, "getUpdates").get("result", [])
        for upd in reversed(updates):
            msg = (
                upd.get("message")
                or upd.get("edited_message")
                or upd.get("callback_query", {}).get("message")
                or {}
            )
            chat = msg.get("chat", {})
            if "id" in chat:
                chat_id = str(chat["id"])
                CHAT_ID_CACHE_FILE.write_text(chat_id, encoding="utf-8")
                return chat_id

        if not printed_prompt:
            print(f"\n👉 First-time setup: Send 'hi' to @{bot_username} once to save your chat_id...\n")
            printed_prompt = True
        time.sleep(2)

    raise TimeoutError(f"No message received by @{bot_username} within {timeout_seconds}s.")


# ---------------------------------------------------------------------------
# 5. Single-Place Bottom Keyboards, Strict Command Matcher & Card Formatters
# ---------------------------------------------------------------------------
BRANCH_BUTTONS = {
    1: "🏬 Branch 1: Maadi (Cairo)",
    2: "🏬 Branch 2: Nasr City (Cairo)",
    3: "🏬 Branch 3: 6th of October (Giza)",
}


def get_branch_selection_keyboard() -> Dict[str, Any]:
    return {
        "keyboard": [
            [{"text": BRANCH_BUTTONS[1]}],
            [{"text": BRANCH_BUTTONS[2]}],
            [{"text": BRANCH_BUTTONS[3]}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def get_branch_services_keyboard() -> Dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📊 Sales Report (PDF)"}, {"text": "🏷️ Active Offers (PDF)"}],
            [{"text": "📦 Inventory & Signals"}, {"text": "🛡️ Audit & Security"}],
            [{"text": "🚨 Stock Alert"}, {"text": "🔙 Back to Branches"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def match_menu_command(text: str) -> Optional[Tuple[str, Any]]:
    """Matches ONLY exact menu buttons or explicit short commands—never hijacks free-text questions!"""
    raw = text.strip()
    low = re.sub(r"\s+", " ", raw.lower())

    if raw == BRANCH_BUTTONS[1] or low in ("branch 1", "maadi", "maadi branch", "/branch1"):
        return ("BRANCH", 1)
    if raw == BRANCH_BUTTONS[2] or low in ("branch 2", "nasr city", "nasr city branch", "/branch2"):
        return ("BRANCH", 2)
    if raw == BRANCH_BUTTONS[3] or low in ("branch 3", "6th of october", "october", "october branch", "/branch3"):
        return ("BRANCH", 3)

    if low in ("🔙 back to branches", "back to branches", "/start", "/branches", "branches", "menu", "/menu"):
        return ("HOME", None)

    if low in ("📊 sales report (pdf)", "📊 sales report", "sales report (pdf)", "sales report", "/report", "report"):
        return ("SERVICE", "REPORT")
    if low in ("🏷️ active offers (pdf)", "🏷️ active offers", "active offers (pdf)", "active offers", "/offers", "offers"):
        return ("SERVICE", "OFFERS")
    if low in ("📦 inventory & signals", "inventory & signals", "inventory", "/inventory"):
        return ("SERVICE", "INVENTORY")
    if low in ("🛡️ audit & security", "audit & security", "audit", "security", "/audit"):
        return ("SERVICE", "AUDIT")
    if low in ("🚨 stock alert", "stock alert", "/alert", "alert"):
        return ("SERVICE", "ALERT")

    if low in ("/approve", "approve"):
        return ("APPROVE", None)

    return None


def detect_branch_mentioned_in_question(text: str) -> Optional[int]:
    low = text.lower()
    if "maadi" in low or "branch 1" in low:
        return 1
    if "nasr" in low or "branch 2" in low:
        return 2
    if "october" in low or "branch 3" in low:
        return 3
    return None


def format_outbox_card(msg: OutboxMessage) -> str:
    p = msg.payload or {}
    branch_id = int(msg.branch_id)
    branch_name = FALLBACK_BRANCH_META.get(branch_id, {}).get("name", f"Branch {branch_id}")

    if "restock_order_id" in p and "requested_quantity" in p:
        order_id = p["restock_order_id"]
        version = p.get("version", 1)
        qty = int(p["requested_quantity"])
        pid = int(p.get("product_id", 1))
        sid = int(p.get("supplier_id", 1))
        prod = PRODUCTS_BY_ID.get(pid, {})
        sup = SUPPLIERS_BY_ID.get(sid, {})

        prod_name = prod.get("product_name", f"Product #{pid}")
        sku = prod.get("sku", f"SKU-{pid:04d}")
        unit_cost = _money(prod.get("cost_price", "20.23"))
        total_cost = unit_cost * Decimal(qty)
        sup_name = sup.get("supplier_name", f"Supplier #{sid}")
        lead_days = sup.get("shipping_duration_days", 1)
        v_hash = str(p.get("version_hash", ""))
        short_hash = f"{v_hash[:10]}...{v_hash[-6:]}" if len(v_hash) > 18 else v_hash

        return (
            f"✅ PURCHASE ORDER SENT TO SUPPLIER\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏢 Branch: {branch_name} (#{branch_id})\n"
            f"📄 Order Number: PO #{order_id} (Version v{version})\n"
            f"🥛 Product: {prod_name} ({sku})\n"
            f"📦 Approved Quantity: {qty} units\n"
            f"💵 Unit Cost: {unit_cost} EGP\n"
            f"💰 Total Order Value: {total_cost:,.2f} EGP\n"
            f"🚚 Supplier: {sup_name}\n"
            f"📧 Supplier Email: {msg.recipient}\n"
            f"⏱️ Estimated Delivery: {lead_days} Day(s)\n"
            f"🔐 Approval Fingerprint: {short_hash}\n"
            f"🛡️ Status: DISPATCHED & RECORDED IN AUDIT LOG"
        )

    if "last_rejection_reason" in p:
        order_id = p.get("restock_order_id", 1001)
        retries = p.get("retry_count", 3)
        reason = p.get("last_rejection_reason", "Exceeded max rejections")
        return (
            f"🚨 OPERATOR ESCALATION ALERT\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏢 Branch: {branch_name} (#{branch_id})\n"
            f"📄 Order Number: PO #{order_id}\n"
            f"⚠️ Status: HALTED AFTER {retries} REJECTIONS\n"
            f"📝 Last Reviewer Note: {reason}\n"
            f"📧 Escalated To: {msg.recipient}"
        )

    details = "\n".join(f"• {k.replace('_', ' ').title()}: {v}" for k, v in p.items())
    return f"📬 SYSTEM NOTIFICATION ({branch_name})\n━━━━━━━━━━━━━━━━━━━━━━\n{details}"


def format_inventory_signals_card(branch_id: int) -> str:
    m = FALLBACK_BRANCH_META[branch_id]
    prod = PRODUCTS_BY_ID[branch_id]
    sup = next(
        (s for s in PILOT_SUPPLIERS_SEED if s["product_category"] == prod["category"]),
        PILOT_SUPPLIERS_SEED[0],
    )
    trigger = "NEAR_EXPIRY + LOW_STOCK" if prod["days_until_expiry"] <= 3 else "LOW_STOCK"
    return (
        f"📦 BRANCH INVENTORY & STOCK SIGNALS\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 Branch: {m['name']} ({m['code']})\n"
        f"🚨 Flagged Product: {prod['product_name']} ({prod['sku']})\n"
        f"📂 Category: {prod['category']} ➔ {prod['subcategory']}\n"
        f"📊 Current Stock on Shelf: {prod['stock_quantity']} units (Demand: {prod['demand_level']})\n"
        f"⏳ Days Until Expiry: {prod['days_until_expiry']} day(s)\n"
        f"⚡ Alert Reason: {trigger}\n"
        f"📥 AI Recommended Order: {prod['recommended_restock_qty']} units\n"
        f"🚚 Verified Supplier: {sup['supplier_name']} ({sup['shipping_duration_days']}d delivery)"
    )


def _translate_audit_entry(rec: Any) -> str:
    action = str(rec.action)
    time_str = str(rec.timestamp)[11:19] if len(str(rec.timestamp)) >= 19 else "Now"
    details = rec.details or {}
    if action == "RUN_STARTED":
        return f"🕒 {time_str} — AI started stock check (Draft v{rec.version})"
    if action == "DECISION_REJECT":
        return f"✏️ {time_str} — Manager rejected v{rec.version} ({details.get('reason', 'Quantity adjusted')})"
    if action == "DECISION_APPROVE":
        return f"✅ {time_str} — Manager approved Order v{rec.version} (by {rec.actor})"
    if action == "DELIVERY_SENT":
        return f"📧 {time_str} — Purchase Order v{rec.version} emailed to {details.get('recipient', 'supplier')}"
    if action == "RUN_COMPLETED":
        return f"🏁 {time_str} — Workflow completed & locked (v{rec.version})"
    if action == "RUN_ESCALATED":
        return f"🚨 {time_str} — Order halted after 3 rejections & sent to Operator"
    return f"• {time_str} — {action} by {rec.actor} (v{rec.version})"


def format_audit_security_card(harness: DuplicateSchedulerAndStaleDataHarness, branch_id: int) -> str:
    m = FALLBACK_BRANCH_META[branch_id]
    entries = harness.audit.list_entries(branch_id=branch_id)
    timeline_lines = [_translate_audit_entry(rec) for rec in entries[-6:]] or [
        "• No orders approved or rejected in this session yet."
    ]
    return (
        f"🛡️ BRANCH SECURITY & ACTIVITY LOG\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 Branch: {m['name']} (#{branch_id})\n\n"
        f"🔐 ACTIVE SECURITY SHIELDS:\n"
        f"1️⃣ Branch Privacy Lock: Only {m['name']} data is accessible here.\n"
        f"2️⃣ Fake-Email Blocker: AI cannot override supplier or manager emails.\n"
        f"3️⃣ Tamper-Proof Approvals: SHA-256 locks the exact approved quantity.\n"
        f"4️⃣ Anti-Duplicate Shield: Zero duplicate supplier emails or reports.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📜 LIVE ACTIVITY HISTORY ({len(entries)} events):\n"
        + "\n".join(timeline_lines)
    )


def compute_suggested_reduction(current_qty: int) -> int:
    if current_qty > 25:
        return current_qty - 20
    if current_qty > 1:
        return max(1, current_qty // 2)
    return 1


def parse_reject_quantity_and_reason(raw_text: str, fallback_qty: int) -> Tuple[int, str]:
    cleaned = re.sub(r"^/?reject\b", "", raw_text.strip(), flags=re.IGNORECASE).strip()
    if not cleaned:
        return fallback_qty, f"Adjusted order quantity to {fallback_qty} units via Telegram"

    num_match = re.search(r"(?<![\w.])(-?\d+(?:\.\d+)?)(?![\w.])", cleaned)
    if num_match is None:
        return fallback_qty, cleaned

    num_str = num_match.group(1)
    if "." in num_str:
        raise InputValidationError(
            "requested_quantity",
            num_str,
            "Quantity must be a whole positive integer (decimals are not allowed).",
        )

    parsed_int = int(num_str)
    InputValidator.validate_positive_int(parsed_int, "requested_quantity")
    reason_part = cleaned[len(num_str) :].strip(" -:,") if cleaned.startswith(num_str) else cleaned
    if not reason_part:
        reason_part = f"Adjusted order quantity to {parsed_int} units via Telegram"
    return parsed_int, reason_part


# ---------------------------------------------------------------------------
# 6. Main Bot Controller (Retail AI + Live Supabase + On-Demand DB PDFs + Memory)
# ---------------------------------------------------------------------------
class RetailOpsTelegramController:
    """Runs the Branch-First Dashboard, AI Analyst, On-Demand DB PDF Generator, and HITL Alerts."""

    def __init__(self, bot_token: str, chat_id: str, api_key: Optional[str] = None) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.harness = DuplicateSchedulerAndStaleDataHarness()
        self.data_provider = LiveBranchDataProvider()
        self.llm = RetailAIAgent(api_key=api_key)

        self.current_branch_id: Optional[int] = None
        self.active_run_id: Optional[str] = None
        self.active_alert_branch_id: int = 1
        self.run_counter = 1
        self.awaiting_custom_reject = False
        self.chat_history: List[Dict[str, str]] = []

        print(
            f"[AI Agent] Status: "
            f"{'ACTIVE (' + self.llm.model + ')' if self.llm.is_enabled else 'NOT SET (add API key in .env)'}"
        )
        print(
            f"[Database] Supabase status: "
            f"{'CONNECTED (' + self.data_provider.supabase_url + ')' if self.data_provider.is_connected else 'PILOT SEED MODE'}"
        )

    def show_branch_selection_home(self) -> None:
        self.current_branch_id = None
        self.chat_history.clear()
        ai_status = (
            f"🤖 AI Agent: Active ({self.llm.model})"
            if self.llm.is_enabled
            else "🤖 AI Agent: Fallback (Add API key in .env)"
        )
        db_status = (
            "🗄️ Database: Live Supabase Connected"
            if self.data_provider.is_connected
            else "🗄️ Database: Pilot Seed Mode"
        )
        home_text = (
            "🏪 DEPI SUPERMARKET AI — BRANCH SELECTOR\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{ai_status}\n"
            f"{db_status}\n"
            "📄 PDF Exports: Enabled (Sales, Offers & Database Queries)\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Please select a branch from the bottom menu below:"
        )
        send_telegram_message(
            self.bot_token,
            self.chat_id,
            home_text,
            reply_markup=get_branch_selection_keyboard(),
        )

    def select_branch(self, branch_id: int) -> None:
        if self.current_branch_id != branch_id:
            self.chat_history.clear()
        self.current_branch_id = branch_id
        m = FALLBACK_BRANCH_META[branch_id]
        prod = PRODUCTS_BY_ID[branch_id]
        branch_text = (
            f"🏢 {m['name'].upper()} ({m['code']} — {m['city']})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Active Branch Locked (Branch #{branch_id})\n"
            f"👤 Manager: {m['approver']}\n"
            f"📦 Flagged Item: {prod['product_name']} ({prod['stock_quantity']} in stock, {prod['days_until_expiry']}d left)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 Pick a service below, or ask ANY database question about {m['name']} (delivered as text + PDF)!"
        )
        send_telegram_message(
            self.bot_token,
            self.chat_id,
            branch_text,
            reply_markup=get_branch_services_keyboard(),
        )

    def push_proactive_restock_alert(self, branch_id: Optional[int] = None) -> None:
        target_branch = branch_id or self.current_branch_id or 1
        self.active_alert_branch_id = target_branch
        self.run_counter += 1
        run_id = f"restock-b{target_branch}-live-{self.run_counter}"
        self.active_run_id = run_id
        self.awaiting_custom_reject = False

        ckpt = self.harness.restock_workflow.start_daily_run(
            actor_id=f"system.scheduler.branch{target_branch}",
            branch_id=target_branch,
            run_id=run_id,
        )
        self._send_hitl_review_card(ckpt, target_branch)

    def _send_hitl_review_card(self, ckpt: Any, branch_id: int) -> None:
        draft = ckpt.state["drafted_report"]
        branch_name = FALLBACK_BRANCH_META[branch_id]["name"]
        current_qty = int(draft["requested_quantity"])
        suggested_qty = compute_suggested_reduction(current_qty)
        retry_count = int(ckpt.state.get("retry_count", 0))
        prod = PRODUCTS_BY_ID.get(int(draft["product_id"]), {})
        unit_cost = _money(prod.get("cost_price", "20.23"))
        est_total = unit_cost * Decimal(current_qty)
        ai_note = self.llm.explain_restock_alert(branch_name, draft, prod)

        prompt_text = (
            f"🚨 URGENT STOCK ALERT — {branch_name.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📄 Draft Version: v{ckpt.state['version']} (Rejections used: {retry_count}/3)\n"
            f"🥛 Product: {draft['product_name']} ({draft['sku']})\n"
            f"⚠️ Trigger: {draft['trigger_reason']} ({prod.get('days_until_expiry', 2)}d left, {prod.get('stock_quantity', 18)} on shelf)\n"
            f"🚚 Supplier: {draft['supplier_name']} ({draft['shipping_duration_days']}d delivery)\n"
            f"📦 Proposed Order Quantity: {current_qty} units\n"
            f"💰 Total Cost: {est_total:,.2f} EGP ({unit_cost} EGP/unit)\n"
            f"📝 Last Feedback: {draft.get('user_feedback') or 'Initial AI Recommendation'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 AI Recommendation ({self.llm.model}):\n{ai_note}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 Approve or Reject below (or type any custom quantity like {suggested_qty}):"
        )

        buttons: List[List[Dict[str, str]]] = [
            [{"text": f"✅ Approve Order ({current_qty} units)", "callback_data": "HITL:APPROVE"}]
        ]
        if suggested_qty < current_qty:
            buttons.append(
                [
                    {
                        "text": f"📉 Reject & Reduce ({current_qty} ➔ {suggested_qty} units)",
                        "callback_data": f"HITL:REJECT:{suggested_qty}:Reduced order from {current_qty} to {suggested_qty} units",
                    }
                ]
            )
        buttons.append([{"text": "✏️ Reject (Type Custom Quantity)", "callback_data": "HITL:ASK_CUSTOM"}])

        send_telegram_message(
            self.bot_token,
            self.chat_id,
            prompt_text,
            reply_markup={"inline_keyboard": buttons},
        )

    def _handle_hitl_decision(
        self,
        *,
        decision: str,
        reason: str,
        override_qty: Optional[int] = None,
    ) -> None:
        if not self.active_run_id:
            send_telegram_message(
                self.bot_token,
                self.chat_id,
                "ℹ️ No active stock alert is waiting for review. Tap '🚨 Stock Alert' to trigger one.",
            )
            return

        branch_id = self.active_alert_branch_id
        approver = FALLBACK_BRANCH_META[branch_id]["approver"]
        prev_email_count = len(self.harness.dispatched_emails)

        ckpt = self.harness.restock_workflow.submit_human_review(
            run_id=self.active_run_id,
            branch_id=branch_id,
            reviewer_actor_id=approver,
            decision=decision,
            reason=reason,
            override_requested_quantity=override_qty,
        )
        self.awaiting_custom_reject = False

        if ckpt.status == "INTERRUPTED":
            self._send_hitl_review_card(ckpt, branch_id)
            return

        self.active_run_id = None
        self.harness.flush_outbox(branch_id=branch_id)
        new_emails = self.harness.dispatched_emails[prev_email_count:]
        active_kb = (
            get_branch_services_keyboard()
            if self.current_branch_id is not None
            else get_branch_selection_keyboard()
        )
        for msg in new_emails:
            card = format_outbox_card(msg)
            print("\n" + card)
            send_telegram_message(self.bot_token, self.chat_id, card, reply_markup=active_kb)

    def handle_service_request(self, service: str) -> None:
        if self.current_branch_id is None:
            send_telegram_message(
                self.bot_token,
                self.chat_id,
                "👆 Please select one of the 3 branches from the bottom menu first!",
                reply_markup=get_branch_selection_keyboard(),
            )
            return

        b_id = self.current_branch_id
        kb = get_branch_services_keyboard()

        if service == "REPORT":
            send_telegram_message(
                self.bot_token,
                self.chat_id,
                f"⏳ Generating Daily Sales Report PDF for {FALLBACK_BRANCH_META[b_id]['name']}...",
                reply_markup=kb,
            )
            data = self.data_provider.get_sales_report_data(b_id)
            ai_summary, ai_badge = self.llm.summarize_sales_report(data)
            pdf_path = generate_sales_report_pdf(data, ai_summary, ai_badge)

            caption = (
                f"📊 {data['branch_name']} — {data['period_label']}\n"
                f"💰 Net Revenue: {data['net_revenue']} EGP | Profit: {data['gross_profit']} EGP ({data['margin_pct']}%)\n"
                f"🗄️ {data['rows_analyzed']:,} rows in day | {data['total_branch_rows']:,} total branch rows\n"
                f"🤖 AI Analyst: {ai_badge}"
            )
            send_telegram_document(self.bot_token, self.chat_id, pdf_path, caption=caption, reply_markup=kb)
            print(f"[PDF] Sent {pdf_path.name} to Telegram chat {self.chat_id}")

        elif service == "OFFERS":
            send_telegram_message(
                self.bot_token,
                self.chat_id,
                f"⏳ Generating Active Offers Strategy PDF for {FALLBACK_BRANCH_META[b_id]['name']}...",
                reply_markup=kb,
            )
            data = self.data_provider.get_active_offers_data(b_id)
            ai_analysis, ai_badge = self.llm.analyze_active_offers(data)
            pdf_path = generate_active_offers_pdf(data, ai_analysis, ai_badge)

            caption = (
                f"🏷️ {data['branch_name']} — Active Offers Report (Offer #{data['offer_id']})\n"
                f"🔥 {data['product_name']}: {data['offer_price']} EGP ({data['discount_pct']}% OFF | Margin: {data['live_margin_pct']}%)\n"
                f"🤖 AI Strategist: {ai_badge}\n"
                f"📎 Open the attached PDF for full guardrails & strategic analysis."
            )
            send_telegram_document(self.bot_token, self.chat_id, pdf_path, caption=caption, reply_markup=kb)
            print(f"[PDF] Sent {pdf_path.name} to Telegram chat {self.chat_id}")

        elif service == "INVENTORY":
            send_telegram_message(self.bot_token, self.chat_id, format_inventory_signals_card(b_id), reply_markup=kb)

        elif service == "AUDIT":
            send_telegram_message(self.bot_token, self.chat_id, format_audit_security_card(self.harness, b_id), reply_markup=kb)

        elif service == "ALERT":
            self.push_proactive_restock_alert(branch_id=b_id)

    def handle_free_text_question(self, text: str) -> None:
        """Handles period sales reports AND general database Q&A with chat memory and PDF exports."""
        mentioned_branch = detect_branch_mentioned_in_question(text)

        if self.current_branch_id is not None and mentioned_branch is not None and mentioned_branch != self.current_branch_id:
            cur_name = FALLBACK_BRANCH_META[self.current_branch_id]["name"]
            req_name = FALLBACK_BRANCH_META[mentioned_branch]["name"]
            send_telegram_message(
                self.bot_token,
                self.chat_id,
                f"🔒 Branch Isolation Active: You are currently locked to {cur_name}.\n"
                f"To inspect {req_name}, tap '🔙 Back to Branches' and select {req_name} first.",
                reply_markup=get_branch_services_keyboard(),
            )
            return

        active_b = self.current_branch_id or mentioned_branch or 1
        kb = get_branch_services_keyboard() if self.current_branch_id else get_branch_selection_keyboard()
        send_typing_action(self.bot_token, self.chat_id)

        # 1. Check if the manager asked for a period-specific Sales Report (e.g., "last month sales report")
        period_spec = parse_sales_period_request(text)
        if period_spec is not None:
            b_name = FALLBACK_BRANCH_META[active_b]["name"]
            send_telegram_message(
                self.bot_token,
                self.chat_id,
                f"⏳ Querying ALL matching Supabase sales rows for {b_name} ({period_spec['kind'].replace('_', ' ')})...",
                reply_markup=kb,
            )
            data = self.data_provider.get_sales_report_data(active_b, period_spec=period_spec)
            ai_summary, ai_badge = self.llm.summarize_sales_report(data)
            pdf_path = generate_sales_report_pdf(data, ai_summary, ai_badge)

            summary_card = (
                f"📊 {data['report_title']}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 Branch: {data['branch_name']} ({data['branch_code']})\n"
                f"📅 Period: {data['period_label']}\n"
                f"🗄️ Coverage: {data['data_source']}\n"
                f"📆 DB Date Range: {data['db_date_range']} (col: {data['date_column_used']})\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Gross Sales: {data['gross_sales']} EGP\n"
                f"💵 Net Revenue: {data['net_revenue']} EGP\n"
                f"📈 Gross Profit: {data['gross_profit']} EGP (Margin: {data['margin_pct']}% | {data['cogs_source']})\n"
                f"🧾 Transactions: {data['transactions']:,} | Units Sold: {data['units_sold']:,}\n"
                f"🛒 Avg Transaction Value: {data['atv']} EGP\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 AI Summary:\n{ai_summary}"
            )
            send_telegram_message(self.bot_token, self.chat_id, summary_card, reply_markup=kb)

            caption = (
                f"📊 {data['branch_name']} — {data['period_label']}\n"
                f"💰 Net Revenue: {data['net_revenue']} EGP | Profit: {data['gross_profit']} EGP ({data['margin_pct']}%)\n"
                f"🗄️ {data['rows_analyzed']:,} rows analyzed out of {data['total_branch_rows']:,} branch rows."
            )
            send_telegram_document(self.bot_token, self.chat_id, pdf_path, caption=caption, reply_markup=kb)
            print(f"[PDF] Sent period sales report {pdf_path.name} ({data['rows_analyzed']} rows)")

            self.chat_history.append({"role": "user", "content": text})
            self.chat_history.append({"role": "assistant", "content": summary_card})
            if len(self.chat_history) > 6:
                self.chat_history = self.chat_history[-6:]
            return

        # 2. General Database & Operational Q&A -> Live Supabase + AI + Executive DB Query PDF
        needed_receipts = extract_requested_count(text, default_count=10, max_count=25)
        branch_ctx = self.data_provider.get_rich_branch_context(
            active_b,
            harness=self.harness,
            receipt_limit=needed_receipts,
        )
        ai_reply = self.llm.answer_branch_question(text, branch_ctx, chat_history=self.chat_history)

        self.chat_history.append({"role": "user", "content": text})
        self.chat_history.append({"role": "assistant", "content": ai_reply})
        if len(self.chat_history) > 6:
            self.chat_history = self.chat_history[-6:]

        if is_casual_smalltalk(text):
            send_telegram_message(
                self.bot_token,
                self.chat_id,
                f"🤖 Retail AI Assistant ({FALLBACK_BRANCH_META[active_b]['name']}):\n━━━━━━━━━━━━━━━━━━━━━━\n{ai_reply}",
                reply_markup=kb,
            )
            return

        send_telegram_message(
            self.bot_token,
            self.chat_id,
            f"🤖 Retail AI Assistant ({FALLBACK_BRANCH_META[active_b]['name']}):\n━━━━━━━━━━━━━━━━━━━━━━\n{ai_reply}",
            reply_markup=kb,
        )

        pdf_question, pdf_answer = self.llm.prepare_pdf_english_content(
            question=text,
            ai_answer=ai_reply,
            branch_name=FALLBACK_BRANCH_META[active_b]["name"],
        )
        explicit_count = extract_explicit_count(text)
        pdf_path = generate_db_query_pdf(
            question=pdf_question,
            ai_answer=pdf_answer,
            branch_ctx=branch_ctx,
            ai_badge=self.llm.model_badge,
            original_question=text,
            display_limit=explicit_count,
        )
        caption = (
            f"📄 Database Query Report — {FALLBACK_BRANCH_META[active_b]['name']}\n"
            f"🔎 Request: {text[:120]}\n"
            f"📎 Attached PDF contains the AI summary & live database records."
        )
        send_telegram_document(self.bot_token, self.chat_id, pdf_path, caption=caption, reply_markup=kb)
        print(f"[PDF] Sent DB query export {pdf_path.name} to Telegram chat {self.chat_id}")

    def serve_forever(self) -> None:
        self.show_branch_selection_home()
        self.push_proactive_restock_alert(branch_id=1)

        initial_updates = telegram_api_call(self.bot_token, "getUpdates").get("result", [])
        last_update_id = initial_updates[-1]["update_id"] if initial_updates else 0
        print("\n[Telegram] Bot running! Press Ctrl+C to stop.")

        while True:
            time.sleep(1.5)
            try:
                updates = telegram_api_call(
                    self.bot_token,
                    "getUpdates",
                    {"offset": last_update_id + 1},
                ).get("result", [])
            except Exception as exc:
                print(f"[Telegram] Polling retry after error: {exc}")
                time.sleep(3)
                continue

            for upd in updates:
                last_update_id = upd["update_id"]

                # 1. Inline buttons (Stock Alert Approve / Reject ONLY)
                cb = upd.get("callback_query")
                if cb:
                    cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    if cb_chat_id != self.chat_id:
                        continue
                    data_str = cb.get("data", "")
                    telegram_api_call(self.bot_token, "answerCallbackQuery", {"callback_query_id": cb["id"]})

                    if data_str == "HITL:APPROVE":
                        self._handle_hitl_decision(decision="APPROVE", reason="Approved via @Retail_ops_bot")
                        continue
                    if data_str.startswith("HITL:REJECT:"):
                        _, _, qty_s, reason_s = data_str.split(":", 3)
                        self._handle_hitl_decision(decision="REJECT", reason=reason_s, override_qty=int(qty_s))
                        continue
                    if data_str == "HITL:ASK_CUSTOM":
                        self.awaiting_custom_reject = True
                        send_telegram_message(
                            self.bot_token,
                            self.chat_id,
                            "✏️ Type the new quantity (e.g. 25 or 25 Limited shelf space):",
                        )
                        continue

                # 2. Bottom Menu Buttons & Free-Text Messages
                msg = upd.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != self.chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                low = text.lower()

                cmd = match_menu_command(text)
                if cmd is not None:
                    kind, val = cmd
                    if kind == "BRANCH":
                        self.select_branch(int(val))
                    elif kind == "HOME":
                        self.show_branch_selection_home()
                    elif kind == "SERVICE":
                        self.handle_service_request(str(val))
                    elif kind == "APPROVE":
                        self._handle_hitl_decision(decision="APPROVE", reason="Approved via @Retail_ops_bot")
                    continue

                is_direct_number = bool(re.match(r"^-?\d+(?:\.\d+)?(?:\s+.*)?$", text))
                if self.active_run_id and (
                    self.awaiting_custom_reject
                    or is_direct_number
                    or low.startswith("/reject")
                    or low.startswith("reject")
                ):
                    if low in ("/reject", "reject") and not self.awaiting_custom_reject:
                        self.awaiting_custom_reject = True
                        send_telegram_message(
                            self.bot_token,
                            self.chat_id,
                            "✏️ Type the new quantity (e.g. 25 or 25 Shelf space limited):",
                        )
                        continue
                    try:
                        override_qty, reason_str = parse_reject_quantity_and_reason(text, fallback_qty=20)
                    except InputValidationError as exc:
                        send_telegram_message(
                            self.bot_token,
                            self.chat_id,
                            f"❌ Invalid Quantity Blocked: {exc}\nPlease reply with a positive whole number (e.g. 25).",
                        )
                        self.awaiting_custom_reject = True
                        continue
                    self._handle_hitl_decision(decision="REJECT", reason=reason_str, override_qty=override_qty)
                    continue

                # 3. Free-Text Manager Q&A -> Live Supabase + AI Assistant + Executive PDF!
                self.handle_free_text_question(text)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run @Retail_ops_bot with AI Assistant, Supabase & PDF Reports.")
    parser.add_argument("--token", default=DEFAULT_BOT_TOKEN, help="Telegram Bot Token")
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID"), help="Optional Telegram Chat ID")
    parser.add_argument("--api-key", default=None, help="Optional LLM API Key")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args(argv)

    chat_id = resolve_chat_id(args.token, explicit_chat_id=args.chat_id)
    controller = RetailOpsTelegramController(args.token, str(chat_id), api_key=args.api_key)
    try:
        controller.serve_forever()
    except KeyboardInterrupt:
        print("\n[Telegram] Bot stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
