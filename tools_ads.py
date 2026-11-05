"""
tools_ads.py — turn a saved campaign draft (tools_creator.plan_ad_campaign) into real,
PAUSED campaigns on Google Ads, Meta (Instagram / click-to-WhatsApp) and TikTok.

Guard rails (these are the point of the module):
  1. Everything is created PAUSED / DISABLED. Nothing here can start spending; you review and
     switch it on yourself in the platform's own UI. (There is deliberately no "activate" function.)
  2. Two steps. prepare_ad_push() only PREVIEWS and stages. Creating anything needs your own
     'confirm ad push' message; confirm_ad_push() is never exposed to the language model.
  3. The ad account's currency is read from the platform and must equal the plan's currency.
     A mismatch (or a currency we can't verify) refuses the push: a "300" planned in NGN must
     never become $300.
  4. Optional hard cap: AGATHA_AD_MAX_BUDGET (in the plan's currency) refuses bigger plans.
  5. One push per campaign per platform (no accidental duplicates).
  6. Secrets are scrubbed from every error message.

What each platform gets (be realistic about it):
  Google Ads  campaign + budget + ad group + keywords + negative keywords + a responsive search
              ad, created atomically in one request. Locations/languages/conversion tracking are
              NOT set; set them before enabling.
  Meta        campaign + ad set (budget, schedule, country targeting, Instagram or Facebook+Instagram,
              or a click-to-WhatsApp destination). The ad creative (image/video) is added in
              Ads Manager. Interests are not set (the plan lists suggestions).
  TikTok      campaign shell only (objective, budget). Ad groups need objective-specific targeting and
              a video, which you add in TikTok Ads Manager.

Written against: Google Ads API v25 (python 'google-ads'), Meta Graph API v25.0, TikTok Business API v1.3.
Not yet exercised against live accounts: preview first, and run your first push against a small plan.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from tools_settings import get_setting as _gs
except Exception:
    def _gs(key, default=None):
        return os.getenv(key, default)

import tools_creator as _tc

META_BASE = "https://graph.facebook.com"
TIKTOK_BASE = "https://business-api.tiktok.com/open_api/v1.3"
HTTP_TIMEOUT = 30
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "PYG", "UGX", "XAF", "XOF", "ISK"}

_pending_push = {}


class PushError(RuntimeError):
    """A push that failed part-way: `created` lists what now exists (all still paused)."""
    def __init__(self, msg, created=None):
        super().__init__(msg)
        self.created = created or {}


_ALIASES = {"google": "google_ads", "google ads": "google_ads", "google_ads": "google_ads", "adwords": "google_ads",
            "meta": "meta", "facebook": "meta", "instagram": "meta", "whatsapp": "meta", "meta_ads": "meta",
            "tiktok": "tiktok_ads", "tiktok ads": "tiktok_ads", "tiktok_ads": "tiktok_ads"}
_LABEL = {"google_ads": "Google Ads", "meta": "Meta Ads", "tiktok_ads": "TikTok Ads"}
_NEEDS = {
    "google_ads": ["GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET",
                   "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_CUSTOMER_ID"],
    "meta": ["META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID"],
    "tiktok_ads": ["TIKTOK_ADS_ACCESS_TOKEN", "TIKTOK_ADVERTISER_ID"],
}


def _setting(key, default=None):
    v = _gs(key, default)
    return v.strip() if isinstance(v, str) else v


def _secrets() -> list:
    keys = ["GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN",
            "META_ACCESS_TOKEN", "TIKTOK_ADS_ACCESS_TOKEN"]
    return [s for s in (_setting(k) for k in keys) if s and len(s) > 6]


def _scrub(text) -> str:
    text = str(text)
    for s in _secrets():
        text = text.replace(s, "***")
    return text


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def configured(platform: str):
    missing = [k for k in _NEEDS[platform] if not _setting(k)]
    return (not missing), missing


def ads_status() -> str:
    lines = []
    for p in ("google_ads", "meta", "tiktok_ads"):
        ok, missing = configured(p)
        lines.append(f"- {_LABEL[p]}: " + ("connected" if ok else "not set up (needs " + ", ".join(missing) + ")"))
    cap = _setting("AGATHA_AD_MAX_BUDGET")
    lines.append(f"- Spend cap per push: {cap if cap else 'none set (AGATHA_AD_MAX_BUDGET)'}")
    lines.append("- Everything I create is PAUSED; you switch it on in the platform.")
    return "Ad accounts:\n" + "\n".join(lines)


def _resolve_platform(text: str):
    t = (text or "").strip().lower()
    for word, key in _ALIASES.items():
        if word in t:
            return key
    return None


# =================================================================== plan data ==
def _plan_row(c: dict, platform: str):
    return next((r for r in c["budget_plan"]["rows"] if r["platform"] == platform), None)


def _dates(c: dict):
    start = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=int(c["duration_days"])) - timedelta(seconds=1)
    return start, end


def _name(c: dict) -> str:
    return f"Agatha | {c['product'][:40]} | {c['id']}"


# ====================================================================== GOOGLE ==
def _google_spec(c: dict, row: dict):
    g = c["copy"].get("google_ads", {})
    if not c.get("landing_url"):
        return None, "what landing page URL (https://...) should the ads send people to?"
    if len(g.get("headlines", [])) < 3 or len(g.get("descriptions", [])) < 2:
        return None, None  # caller reports the copy problem
    start, end = _dates(c)
    return {"name": _name(c), "daily": row["daily"], "url": c["landing_url"], "headlines": g["headlines"][:15],
            "descriptions": g["descriptions"][:4], "keywords": g.get("keywords", [])[:20],
            "negatives": g.get("negative_keywords", [])[:10], "start": start, "end": end}, None


def google_operations(client, customer_id: str, spec: dict) -> list:
    """Pure builder (no network): one atomic batch. Temp resource ids are negative numbers."""
    cid = _digits(customer_id)
    budget_rn, campaign_rn, group_rn = (f"customers/{cid}/campaignBudgets/-1", f"customers/{cid}/campaigns/-2",
                                        f"customers/{cid}/adGroups/-3")
    ops = []

    op = client.get_type("MutateOperation")
    b = op.campaign_budget_operation.create
    b.resource_name = budget_rn
    b.name = spec["name"] + " budget"
    b.amount_micros = int(round(spec["daily"] * 1_000_000))
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    b.explicitly_shared = False
    ops.append(op)

    op = client.get_type("MutateOperation")
    c = op.campaign_operation.create
    c.resource_name = campaign_rn
    c.name = spec["name"]
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.status = client.enums.CampaignStatusEnum.PAUSED                 # never starts on its own
    c.campaign_budget = budget_rn
    client.copy_from(c.target_spend, client.get_type("TargetSpend"))  # "Maximize clicks": needs no conversion tracking
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = True
    c.network_settings.target_content_network = False
    c.network_settings.target_partner_search_network = False
    c.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    c.start_date_time = spec["start"].strftime("%Y-%m-%d %H:%M:%S")
    c.end_date_time = spec["end"].strftime("%Y-%m-%d %H:%M:%S")
    ops.append(op)

    op = client.get_type("MutateOperation")
    ag = op.ad_group_operation.create
    ag.resource_name = group_rn
    ag.name = "Core"
    ag.campaign = campaign_rn
    ag.status = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ops.append(op)

    for kw in spec["keywords"]:
        for match in ("PHRASE", "EXACT"):
            op = client.get_type("MutateOperation")
            crit = op.ad_group_criterion_operation.create
            crit.ad_group = group_rn
            crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            crit.keyword.text = kw
            crit.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match)
            ops.append(op)

    for kw in spec["negatives"]:
        op = client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = campaign_rn
        crit.negative = True
        crit.keyword.text = kw
        crit.keyword.match_type = client.enums.KeywordMatchTypeEnum.PHRASE
        ops.append(op)

    op = client.get_type("MutateOperation")
    aga = op.ad_group_ad_operation.create
    aga.ad_group = group_rn
    aga.status = client.enums.AdGroupAdStatusEnum.ENABLED   # the CAMPAIGN is paused, so nothing serves
    aga.ad.final_urls.append(spec["url"])
    for text in spec["headlines"]:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        aga.ad.responsive_search_ad.headlines.append(asset)
    for text in spec["descriptions"]:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        aga.ad.responsive_search_ad.descriptions.append(asset)
    ops.append(op)
    return ops


def _google_client():
    from google.ads.googleads.client import GoogleAdsClient
    cfg = {"developer_token": _setting("GOOGLE_ADS_DEVELOPER_TOKEN"), "client_id": _setting("GOOGLE_ADS_CLIENT_ID"),
           "client_secret": _setting("GOOGLE_ADS_CLIENT_SECRET"), "refresh_token": _setting("GOOGLE_ADS_REFRESH_TOKEN"),
           "use_proto_plus": True}
    login = _digits(_setting("GOOGLE_ADS_LOGIN_CUSTOMER_ID"))
    if login:
        cfg["login_customer_id"] = login
    return GoogleAdsClient.load_from_dict(cfg, version=_setting("GOOGLE_ADS_API_VERSION", "v25"))


def _google_account():
    """-> (currency, name). Read-only."""
    client = _google_client()
    svc = client.get_service("GoogleAdsService")
    rows = svc.search(customer_id=_digits(_setting("GOOGLE_ADS_CUSTOMER_ID")),
                      query="SELECT customer.currency_code, customer.descriptive_name FROM customer LIMIT 1")
    for r in rows:
        return r.customer.currency_code, r.customer.descriptive_name
    raise RuntimeError("Google Ads returned no account details")


def _google_push(job: dict) -> dict:
    from google.ads.googleads.errors import GoogleAdsException
    client = _google_client()
    cid = _digits(_setting("GOOGLE_ADS_CUSTOMER_ID"))
    ops = google_operations(client, cid, job["spec"])
    try:
        resp = client.get_service("GoogleAdsService").mutate(customer_id=cid, mutate_operations=ops)
    except GoogleAdsException as ex:
        msgs = "; ".join(e.message for e in list(ex.failure.errors)[:3])
        raise RuntimeError(f"Google Ads rejected the request, so nothing was created: {msgs}")
    ids = {}
    for r in resp.mutate_operation_responses:
        if r.campaign_result.resource_name:
            ids["campaign"] = r.campaign_result.resource_name.split("/")[-1]
        if r.campaign_budget_result.resource_name:
            ids["budget"] = r.campaign_budget_result.resource_name.split("/")[-1]
        if r.ad_group_result.resource_name:
            ids["ad_group"] = r.ad_group_result.resource_name.split("/")[-1]
    return {"ids": ids, "account": cid, "note": "Set locations, languages and conversion tracking, then enable the campaign in Google Ads."}


# ========================================================================= META ==
def _meta_version() -> str:
    return _setting("META_API_VERSION", "v25.0")


def _meta_call(method: str, path: str, params: dict) -> dict:
    if requests is None:
        raise RuntimeError("the 'requests' package isn't installed")
    url = f"{META_BASE}/{_meta_version()}/{path}"
    body = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in (params or {}).items()}
    body["access_token"] = _setting("META_ACCESS_TOKEN")
    res = requests.request(method, url, params=body if method == "GET" else None,
                           data=body if method != "GET" else None, timeout=HTTP_TIMEOUT)
    try:
        data = res.json()
    except Exception:
        raise RuntimeError(f"Meta returned a non-JSON response ({res.status_code})")
    if res.status_code >= 300 or "error" in data:
        err = data.get("error", {})
        raise RuntimeError(_scrub(f"Meta error {err.get('code', res.status_code)}: {err.get('error_user_msg') or err.get('message', '')[:300]}"))
    return data


def _meta_account():
    acct = _setting("META_AD_ACCOUNT_ID")
    acct = acct if str(acct).startswith("act_") else f"act_{_digits(acct)}"
    data = _meta_call("GET", acct, {"fields": "currency,name,account_status"})
    if data.get("account_status") not in (None, 1):
        raise RuntimeError(f"the Meta ad account isn't active (status {data.get('account_status')})")
    return data.get("currency", ""), data.get("name", ""), acct


def _meta_steps(c: dict, row: dict, countries: list, whatsapp: bool, instagram_only: bool) -> list:
    cur = c["currency"].upper()
    minor = int(round(row["daily"] * (1 if cur in ZERO_DECIMAL else 100)))
    pixel = _setting("META_PIXEL_ID")
    goal = c["goal"]
    start, end = _dates(c)
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

    # Without a pixel the honest objective for sales/leads is traffic; conversion objectives need one.
    if whatsapp:
        objective, opt, destination = "OUTCOME_ENGAGEMENT", "CONVERSATIONS", "WHATSAPP"
        promoted = {"page_id": _setting("META_PAGE_ID"), "whatsapp_phone_number": _setting("META_WHATSAPP_NUMBER")}
    elif goal == "awareness":
        objective, opt, destination, promoted = "OUTCOME_AWARENESS", "REACH", None, None
    elif goal in ("sales", "leads") and pixel:
        objective = "OUTCOME_SALES" if goal == "sales" else "OUTCOME_LEADS"
        opt, destination = "OFFSITE_CONVERSIONS", "WEBSITE"
        promoted = {"pixel_id": pixel, "custom_event_type": "PURCHASE" if goal == "sales" else "LEAD"}
    else:
        objective, opt, destination, promoted = "OUTCOME_TRAFFIC", "LINK_CLICKS", "WEBSITE", None

    adset = {
        "name": _name(c) + " | adset", "campaign_id": "{campaign_id}", "daily_budget": minor,
        "billing_event": "IMPRESSIONS", "optimization_goal": opt, "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "is_adset_budget_sharing_enabled": False,   # required when the budget is set on the ad set (Graph v24+)
        "targeting": {"geo_locations": {"countries": countries}, "age_min": 18, "age_max": 65,
                      "publisher_platforms": ["instagram"] if instagram_only else ["facebook", "instagram"]},
        "start_time": utc(start), "end_time": utc(end), "status": "PAUSED",
    }
    if destination:
        adset["destination_type"] = destination
    if promoted:
        adset["promoted_object"] = promoted
    return [
        {"label": "Campaign", "path": "{acct}/campaigns", "save": "campaign_id",
         "params": {"name": _name(c), "objective": objective, "status": "PAUSED", "special_ad_categories": []}},
        {"label": "Ad set", "path": "{acct}/adsets", "save": "adset_id", "params": adset},
    ]


def _meta_push(job: dict) -> dict:
    cur, name, acct = _meta_account()
    saved, created = {}, {}
    try:
        for step in job["steps"]:
            params = json.loads(json.dumps(step["params"]).replace("{campaign_id}", saved.get("campaign_id", "")))
            data = _meta_call("POST", step["path"].format(acct=acct), params)
            saved[step["save"]] = data.get("id")
            created[step["label"].lower().replace(" ", "_")] = data.get("id")
    except RuntimeError as e:
        done = ", ".join(f"{k} {v}" for k, v in created.items()) or "nothing"
        raise PushError(f"{e} (already created, still PAUSED: {done})", created)
    return {"ids": created, "account": acct,
            "note": "Add your image/video creative in Ads Manager (and interests if you want them), then switch it on."}


# ======================================================================= TIKTOK ==
def _tiktok_call(method: str, path: str, params=None, body=None) -> dict:
    if requests is None:
        raise RuntimeError("the 'requests' package isn't installed")
    res = requests.request(method, f"{TIKTOK_BASE}/{path}", params=params, json=body,
                           headers={"Access-Token": _setting("TIKTOK_ADS_ACCESS_TOKEN")}, timeout=HTTP_TIMEOUT)
    try:
        data = res.json()
    except Exception:
        raise RuntimeError(f"TikTok returned a non-JSON response ({res.status_code})")
    if res.status_code >= 300 or data.get("code") not in (0, None):
        raise RuntimeError(_scrub(f"TikTok error {data.get('code', res.status_code)}: {str(data.get('message', ''))[:300]}"))
    return data.get("data", {}) or {}


def _tiktok_account():
    adv = _setting("TIKTOK_ADVERTISER_ID")
    data = _tiktok_call("GET", "advertiser/info/", params={"advertiser_ids": json.dumps([adv]),
                                                           "fields": json.dumps(["currency", "name", "status"])})
    rows = data.get("list") or []
    if not rows:
        raise RuntimeError("TikTok returned no advertiser details")
    return rows[0].get("currency", ""), rows[0].get("name", ""), adv


def _tiktok_body(c: dict, row: dict) -> dict:
    # Objective can't be changed after creation, so only choose ones that need no pixel/app.
    objective = "REACH" if c["goal"] == "awareness" else "TRAFFIC"
    return {"advertiser_id": _setting("TIKTOK_ADVERTISER_ID"), "campaign_name": _name(c), "objective_type": objective,
            "budget_mode": "BUDGET_MODE_DAY", "budget": row["daily"], "operation_status": "DISABLE"}


def _tiktok_push(job: dict) -> dict:
    data = _tiktok_call("POST", "campaign/create/", body=job["body"])
    return {"ids": {"campaign": data.get("campaign_id")}, "account": job["body"]["advertiser_id"],
            "note": "Add the ad group (targeting, schedule, bid) and your video ad in TikTok Ads Manager, then enable it."}


# ================================================================ prepare / confirm ==
def _ask(question: str) -> str:
    return f"Could not prepare the push: {question}"


def prepare_ad_push(campaign_id: str, platform: str, countries: str = "", destination: str = "") -> str:
    """Preview (and stage) pushing a saved campaign draft to Google Ads, Meta or TikTok. Creates nothing."""
    global _pending_push
    c = _tc._load_campaign(campaign_id)
    if not c:
        return f"Could not prepare the push: no campaign '{campaign_id}'. Say 'list campaigns', or ask me to plan one first."
    p = _resolve_platform(platform)
    if not p:
        return "Could not prepare the push: which platform: Google Ads, Meta (Instagram/WhatsApp), or TikTok?"
    row = _plan_row(c, p)
    if not row:
        return f"Could not prepare the push: campaign {c['id']} has no budget allocated to {_LABEL[p]} (it was dropped or not requested)."
    if c.get("pushed", {}).get(p):
        return f"Could not prepare the push: {c['id']} was already pushed to {_LABEL[p]} (ids: {c['pushed'][p]['ids']}). I won't create a duplicate."
    cap = _setting("AGATHA_AD_MAX_BUDGET")
    try:
        if cap and float(c["budget"]) > float(cap):
            return f"Could not prepare the push: this plan's budget ({_tc._money(c['budget'], c['currency'])}) is above your cap of {cap} (AGATHA_AD_MAX_BUDGET)."
    except ValueError:
        return "Could not prepare the push: AGATHA_AD_MAX_BUDGET isn't a number."

    cur = c["currency"].upper()
    job = {"campaign_id": c["id"], "platform": p}
    lines = [f"Preview: push {c['id']} ('{c['product']}') to {_LABEL[p]}. Nothing is created until you confirm, and everything is created PAUSED."]
    dstart, dend = _dates(c)
    daily = _tc._money(row["daily"], cur)

    if p == "google_ads":
        spec, question = _google_spec(c, row)
        if question:
            return _ask(question)
        if not spec:
            return "Could not prepare the push: this campaign has too little Google search copy (needs 3+ headlines and 2+ descriptions that fit Google's limits). Ask me to plan it again."
        job["spec"] = spec
        lines += [f"- Daily budget {daily} ({_tc._money(row['amount'], cur)} total), Maximize clicks bidding",
                  f"- Search campaign, PAUSED, {dstart:%b %d} to {dend:%b %d}",
                  f"- Ad group 'Core': {len(spec['keywords'])} keywords (phrase + exact), {len(spec['negatives'])} negative keywords",
                  f"- 1 responsive search ad ({len(spec['headlines'])} headlines, {len(spec['descriptions'])} descriptions) to {spec['url']}",
                  "- NOT set by me (set before enabling): locations, languages, conversion tracking"]
    elif p == "meta":
        whatsapp = "whatsapp" in (platform + " " + destination).lower()
        instagram_only = "instagram" in platform.lower() and "facebook" not in platform.lower()
        codes = [x.strip().upper() for x in re.split(r"[,\s]+", countries or _setting("AGATHA_ADS_COUNTRIES") or "") if len(x.strip()) == 2]
        if not codes:
            return _ask("which countries should it target? Give 2-letter codes, e.g. NG, GH, ZA.")
        if whatsapp and not _setting("META_WHATSAPP_NUMBER"):
            return _ask("what is the WhatsApp Business number (international format) for the chat button? Set META_WHATSAPP_NUMBER in .env.")
        if not whatsapp and not c.get("landing_url"):
            return _ask("what landing page URL (https://...) should the ads send people to?")
        job["steps"] = _meta_steps(c, row, codes, whatsapp, instagram_only)
        adset = job["steps"][1]["params"]
        lines += [f"- Campaign objective {job['steps'][0]['params']['objective']}"
                  + ("" if (whatsapp or c['goal'] == 'awareness' or _setting('META_PIXEL_ID') or c['goal'] not in ('sales', 'leads'))
                     else " (a pixel isn't set, so sales/leads run as traffic; set META_PIXEL_ID for conversion optimisation)"),
                  f"- Ad set: {daily}/day, {'Instagram only' if instagram_only else 'Facebook + Instagram'}, countries {', '.join(codes)}, "
                  f"ages 18-65, {dstart:%b %d} to {dend:%b %d}, PAUSED",
                  f"- Destination: {'click-to-WhatsApp chat' if whatsapp else 'your website'} (optimising for {adset['optimization_goal']})",
                  "- NOT included: the ad creative (add image/video in Ads Manager) and interest targeting"]
    else:
        job["body"] = _tiktok_body(c, row)
        lines += [f"- Campaign shell: objective {job['body']['objective_type']}, daily budget {daily}, DISABLED",
                  "- NOT included: ad group (targeting, schedule, bid) and the video ad, which you add in TikTok Ads Manager"]

    for part in c.get("partial_pushes", []):
        if part["platform"] == p:
            lines.append(f"Heads-up: an earlier attempt failed part-way and left PAUSED objects ({part['ids']}). "
                         f"Delete them in {_LABEL[p]} first, or you'll have duplicates.")
    ok, missing = configured(p)
    if not ok:
        lines.append(f"Preview only: {_LABEL[p]} isn't connected yet. Add {', '.join(missing)} to your .env to enable pushing.")
        _pending_push = {}
        return "\n".join(lines)

    try:  # read-only check: the account's currency must match the plan
        acct_cur, acct_name = {"google_ads": _google_account, "meta": lambda: _meta_account()[:2],
                               "tiktok_ads": lambda: _tiktok_account()[:2]}[p]()
    except Exception as e:
        _pending_push = {}
        return ("Could not prepare the push: I couldn't verify the ad account's currency, and I won't risk a wrong-currency budget. "
                f"({_scrub(e)[:200]})")
    if (acct_cur or "").upper() != cur:
        _pending_push = {}
        return (f"Could not prepare the push: the plan is in {cur} but the {_LABEL[p]} account '{acct_name}' bills in {acct_cur or 'an unknown currency'}. "
                f"Re-plan in {acct_cur} so the amounts are real.")
    lines.append(f"Account: {acct_name} ({acct_cur}). Currency matches the plan.")
    _pending_push = job
    lines.append("Say 'confirm ad push' to create it (paused), or 'cancel ad push'.")
    return "\n".join(lines)


def confirm_ad_push() -> str:
    """Only ever called from the user's own 'confirm ad push' message, never by the model."""
    global _pending_push
    if not _pending_push:
        return "There's no staged ad push to confirm."
    job, _pending_push = _pending_push, {}
    p = job["platform"]
    try:
        result = {"google_ads": _google_push, "meta": _meta_push, "tiktok_ads": _tiktok_push}[p](job)
    except Exception as e:
        created = getattr(e, "created", None)
        if created:  # remember the orphans so a retry warns instead of silently duplicating
            c = _tc._load_campaign(job["campaign_id"])
            if c is not None:
                c.setdefault("partial_pushes", []).append({"platform": p, "ids": created,
                                                           "at": datetime.now().isoformat(timespec="minutes")})
                _tc._save_campaign(c)
        return f"The {_LABEL[p]} push failed: {_scrub(e)}"
    c = _tc._load_campaign(job["campaign_id"])
    if c is not None:
        c.setdefault("pushed", {})[p] = {"at": datetime.now().isoformat(timespec="minutes"), "ids": result["ids"],
                                          "account": result["account"], "state": "PAUSED"}
        _tc._save_campaign(c)
    ids = ", ".join(f"{k} {v}" for k, v in result["ids"].items())
    return (f"Created on {_LABEL[p]} (account {result['account']}), all PAUSED, nothing is spending: {ids}. "
            f"{result['note']}")


def cancel_ad_push() -> str:
    global _pending_push
    if not _pending_push:
        return "There's no staged ad push to cancel."
    _pending_push = {}
    return "Discarded. Nothing was created."


def has_pending_push() -> bool:
    return bool(_pending_push)


def list_ad_pushes() -> str:
    import glob
    lines = [ads_status(), ""]
    found = False
    for fp in sorted(glob.glob(os.path.join(_tc.CAMPAIGN_DIR, "c_*.json")), key=os.path.getmtime, reverse=True)[:20]:
        try:
            c = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for plat, info in (c.get("pushed") or {}).items():
            found = True
            lines.append(f"{c['id']} -> {_LABEL.get(plat, plat)} [{info.get('state', 'PAUSED')}] {info['at']}: "
                         + ", ".join(f"{k} {v}" for k, v in info["ids"].items()))
    if not found:
        lines.append("Nothing pushed yet.")
    return "\n".join(lines)


# ============================================== tool registry (merged by main) ==
def _s(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


_STR = {"type": "string"}
TOOL_SCHEMAS = [
    _s("prepare_ad_push", "Preview a saved campaign draft for Google Ads, Meta (Instagram/WhatsApp) or TikTok. Creates nothing; the user must confirm.",
       {"campaign_id": {**_STR, "description": "e.g. c_1a2b3c"},
        "platform": {**_STR, "description": "google, meta, instagram, whatsapp or tiktok"},
        "countries": {**_STR, "description": "Meta only: 2-letter country codes, e.g. NG,GH"},
        "destination": {**_STR, "description": "'whatsapp' for click-to-WhatsApp ads, else website"}},
       ["campaign_id", "platform"]),
    _s("list_ad_pushes", "Show which ad accounts are connected and what has already been pushed.", {}, []),
]
FUNCTIONS = {"prepare_ad_push": prepare_ad_push, "list_ad_pushes": list_ad_pushes}
# confirm_ad_push / cancel_ad_push are deliberately NOT here: only the user's own message may confirm.
TOOL_INTENTS = {
    r"\b(?:push|launch|upload|set ?up)\b.*\b(?:google|meta|facebook|instagram|whatsapp|tiktok)\b|\bad accounts?\b|\bpushed\b":
        {"prepare_ad_push", "list_ad_pushes"},
}
