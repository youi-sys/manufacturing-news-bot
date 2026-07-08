#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
製造業 国内外 最新事例 収集 & メール通知スクリプト
====================================================

sources.yaml に定義したソース（公式RSS or Google Newsの
サイト限定検索RSS）から新着記事を集め、まだ通知していない
ものだけを1通のメールにまとめて送信します。

必要な環境変数（GitHub Secrets等で設定）:
  GMAIL_ADDRESS       : 送信元Gmailアドレス
  GMAIL_APP_PASSWORD  : Gmailのアプリパスワード（16桁）
  MAIL_TO             : 通知先メールアドレス（カンマ区切りで複数可）

任意の環境変数:
  LOOKBACK_HOURS      : 何時間前までの記事を対象にするか（既定 26）
  SOURCES_FILE        : sources.yaml のパス（既定 sources.yaml）
  HISTORY_FILE        : 送信済みURL履歴のパス（既定 data/sent_urls.json）
  MAX_ITEMS_PER_SOURCE: 1ソースあたり最大取得件数（既定 10）
  DRY_RUN             : "1" にするとメール送信せず内容を標準出力するだけ
"""

import os
import time
import smtplib
import ssl
import html
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import json

import feedparser
import yaml
import requests


# ---------------------------------------------------------------
# 設定読み込み
# ---------------------------------------------------------------
SOURCES_FILE = os.environ.get("SOURCES_FILE", "sources.yaml")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "data/sent_urls.json")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))
MAX_ITEMS_PER_SOURCE = int(os.environ.get("MAX_ITEMS_PER_SOURCE", "10"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# Gemini API（無料枠）による「示唆付き要約」設定
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# 1回のAPI呼び出しに含める記事数の上限（無料枠のトークン制限内に収めるため）
GEMINI_MAX_ITEMS = int(os.environ.get("GEMINI_MAX_ITEMS", "40"))

JST = timezone(timedelta(hours=9))


def load_sources():
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_history(url_set, max_keep=5000):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    trimmed = list(url_set)[-max_keep:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)


def build_gnews_url(query, region):
    """Google News RSS検索のURLを組み立てる"""
    if region == "overseas":
        hl, gl, ceid = "en-US", "US", "US:en"
    else:
        hl, gl, ceid = "ja", "JP", "JP:ja"
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def entry_datetime(entry):
    """feedparserのエントリから発行日時(UTC)を推定する"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def matches_keywords(entry, keywords):
    if not keywords:
        return True
    text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
    return any(kw.lower() in text for kw in keywords)


def fetch_source(source):
    """1つのソース定義から新着記事のリストを取得する"""
    stype = source.get("type", "rss")
    if stype == "rss":
        url = source["url"]
    elif stype == "gnews":
        url = build_gnews_url(source["query"], source.get("region", "domestic"))
    else:
        print(f"[WARN] unknown source type: {stype} ({source.get('name')})")
        return []

    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"[ERROR] failed to fetch {source.get('name')}: {e}")
        return []

    if feed.bozo and not feed.entries:
        print(f"[WARN] parse issue for {source.get('name')}: {feed.bozo_exception}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    keywords = source.get("keywords") or []

    results = []
    for entry in feed.entries[: MAX_ITEMS_PER_SOURCE * 3]:
        pub_dt = entry_datetime(entry)
        if pub_dt is not None and pub_dt < cutoff:
            continue
        if not matches_keywords(entry, keywords):
            continue

        link = entry.get("link", "")
        title = html.unescape(entry.get("title", "(タイトルなし)"))
        summary_raw = entry.get("summary", "") or entry.get("description", "")
        summary = html.unescape(summary_raw)

        results.append(
            {
                "source_name": source.get("name", "unknown"),
                "region": source.get("region", "domestic"),
                "category": source.get("category", ""),
                "title": title,
                "link": link,
                "summary": summary,
                "published": pub_dt.astimezone(JST).strftime("%Y-%m-%d %H:%M") if pub_dt else "",
            }
        )
        if len(results) >= MAX_ITEMS_PER_SOURCE:
            break

    return results


def strip_html(text, limit=140):
    """summary内の簡易HTMLタグを除去して短くする"""
    import re

    plain = re.sub(r"<[^>]+>", "", text or "")
    plain = " ".join(plain.split())
    if len(plain) > limit:
        plain = plain[:limit].rstrip() + "…"
    return plain


def call_gemini_summarize(items):
    """
    Gemini API（無料枠）を1回だけ呼び出し、
    (1) 記事ごとの一文要約+コンサル視点の示唆
    (2) 全体を通じた3〜5個の共通トレンド
    をまとめて生成する。

    戻り値: {
        "trends": ["...", "..."],
        "per_item": {"<link>": {"summary": "...", "implication": "..."}}
    }
    失敗時は None を返す（呼び出し側で元のsummaryにフォールバック）。
    """
    if not GEMINI_API_KEY:
        print("[INFO] GEMINI_API_KEY未設定のため、Geminiによる要約をスキップします")
        return None

    target_items = items[:GEMINI_MAX_ITEMS]

    numbered_items = []
    for i, it in enumerate(target_items):
        numbered_items.append(
            {
                "id": i,
                "title": it["title"],
                "source": it["source_name"],
                "region": it["region"],
                "snippet": strip_html(it["summary"], limit=300),
            }
        )

    prompt = f"""あなたは製造業クライアントを担当する経営コンサルタントのリサーチアシスタントです。
以下は本日収集した、国内外の製造業ニュースおよび主要コンサルティングファーム／シンクタンクが
発表したインサイト記事のリストです（JSON配列、各要素にid/title/source/region/snippetを含む）。

# タスク
1. 各記事について、次の2つを日本語で作成してください。
   - summary: 記事内容の一文要約（60字程度、事実ベース、断定しすぎない）
   - implication: コンサルタントが顧客提案・自社の景況感把握に活かせる示唆（so-what）を1〜2文で。
     「なぜ重要か」「どのクライアント/業界に関係しそうか」を意識すること。
2. 記事全体を俯瞰し、共通して見られるトレンドやテーマを3〜5個、それぞれ1〜2文で抽出してください（trends）。
   単なる記事の並べ替えではなく、複数記事に共通する構造的な変化・示唆を書くこと。

# 記事リスト
{json.dumps(numbered_items, ensure_ascii=False)}

# 出力形式
前置き・説明・コードブロック記号（```）は一切付けず、次のJSON形式のみを出力してください:
{{
  "trends": ["トレンド1の説明", "トレンド2の説明", ...],
  "items": [
    {{"id": 0, "summary": "...", "implication": "..."}},
    ...
  ]
}}
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # 念のためコードフェンスが付いていた場合に備えて除去
        cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(cleaned)
    except Exception as e:
        print(f"[WARN] Gemini要約に失敗しました。通常表示にフォールバックします: {e}")
        return None

    per_item = {}
    for entry in parsed.get("items", []):
        idx = entry.get("id")
        if idx is None or not (0 <= idx < len(target_items)):
            continue
        link = target_items[idx]["link"]
        per_item[link] = {
            "summary": entry.get("summary", ""),
            "implication": entry.get("implication", ""),
        }

    return {"trends": parsed.get("trends", []), "per_item": per_item}


def build_email_body(items, gemini_result=None):
    """収集結果からHTMLメール本文を組み立てる"""
    now_str = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")

    domestic = [i for i in items if i["region"] == "domestic"]
    overseas = [i for i in items if i["region"] == "overseas"]

    per_item = (gemini_result or {}).get("per_item", {})
    trends = (gemini_result or {}).get("trends", [])

    def trends_html():
        if not trends:
            return ""
        lis = "".join(f'<li style="margin-bottom:6px;">{html.escape(t)}</li>' for t in trends)
        return (
            '<div style="background:#f4f7fb;border-left:4px solid #0b5fff;padding:12px 16px;margin:16px 0;">'
            '<h2 style="margin:0 0 8px;color:#1a1a1a;font-size:16px;">📊 本日の共通トレンド（AI生成）</h2>'
            f'<ul style="margin:0;padding-left:20px;">{lis}</ul>'
            "</div>"
        )

    def section_html(title, group):
        if not group:
            return ""
        by_cat = {}
        for it in group:
            by_cat.setdefault(it["category"] or "その他", []).append(it)

        parts = [f'<h2 style="margin:24px 0 8px;color:#1a1a1a;">{html.escape(title)}</h2>']
        for cat, cat_items in by_cat.items():
            parts.append(
                f'<h3 style="margin:16px 0 4px;color:#333;font-size:15px;">{html.escape(cat)}</h3>'
            )
            parts.append('<ul style="margin:0 0 8px;padding-left:20px;">')
            for it in cat_items:
                ai = per_item.get(it["link"])
                body_extra = ""
                if ai and (ai.get("summary") or ai.get("implication")):
                    if ai.get("summary"):
                        body_extra += (
                            f'<br><span style="font-size:13px;color:#444;">{html.escape(ai["summary"])}</span>'
                        )
                    if ai.get("implication"):
                        body_extra += (
                            '<br><span style="font-size:13px;color:#0b7a3d;">'
                            f'💡 示唆: {html.escape(ai["implication"])}</span>'
                        )
                else:
                    # Geminiが使えない場合は元のRSS要約にフォールバック
                    summary_txt = strip_html(it["summary"])
                    if summary_txt:
                        body_extra = f'<br><span style="font-size:13px;color:#444;">{html.escape(summary_txt)}</span>'

                parts.append(
                    '<li style="margin-bottom:12px;">'
                    f'<a href="{html.escape(it["link"])}" style="color:#0b5fff;text-decoration:none;font-weight:bold;">'
                    f'{html.escape(it["title"])}</a><br>'
                    f'<span style="color:#666;font-size:12px;">{html.escape(it["source_name"])}'
                    f'{" ・ " + it["published"] if it["published"] else ""}</span>'
                    + body_extra
                    + "</li>"
                )
            parts.append("</ul>")
        return "".join(parts)

    body_html = f"""
    <html><body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;line-height:1.6;">
    <p style="color:#666;font-size:13px;">{now_str} 時点で収集した新着情報です（過去{LOOKBACK_HOURS}時間分・未通知のみ）</p>
    {trends_html()}
    {section_html("国内", domestic)}
    {section_html("海外", overseas)}
    <p style="color:#999;font-size:11px;margin-top:24px;">
    本メールは製造業ニュース自動収集プログラムにより送信されています。要約・示唆はAIが生成したものであり、内容の正確性は原文リンクでご確認ください。
    </p>
    </body></html>
    """
    return body_html


def send_email(subject, html_body):
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    mail_to = [addr.strip() for addr in os.environ["MAIL_TO"].split(",") if addr.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = ", ".join(mail_to)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, mail_to, msg.as_string())


def main():
    sources = load_sources()
    history = load_history()
    all_new_items = []

    print(f"[INFO] loaded {len(sources)} sources / lookback={LOOKBACK_HOURS}h")

    for source in sources:
        items = fetch_source(source)
        new_items = [it for it in items if it["link"] and it["link"] not in history]
        print(f"[INFO] {source.get('name')}: fetched={len(items)} new={len(new_items)}")
        all_new_items.extend(new_items)
        time.sleep(1)  # 連続アクセスを避けるための小休止

    if not all_new_items:
        print("[INFO] no new items. skip sending email.")
        return

    seen = set()
    deduped = []
    for it in all_new_items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        deduped.append(it)

    subject = f"【製造業ニュース】{datetime.now(JST).strftime('%Y/%m/%d')} 新着 {len(deduped)}件"
    gemini_result = call_gemini_summarize(deduped)
    body_html = build_email_body(deduped, gemini_result)

    if DRY_RUN:
        print("===== DRY RUN: メール本文 =====")
        print(subject)
        print(body_html)
    else:
        send_email(subject, body_html)
        print(f"[INFO] email sent: {len(deduped)} items")

    history.update(seen)
    save_history(history)


if __name__ == "__main__":
    main()
