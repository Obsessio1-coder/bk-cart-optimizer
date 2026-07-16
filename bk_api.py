import os, sys, json, requests, re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "bk_all_menus")
STRUCT_PATH = os.path.join(BASE_DIR, "combo_structures.json")

SESSION_HEADERS = {
    "x-burgerking-platform": "web_mobile",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "accept": "application/json",
    "content-type": "application/json",
    "accept-language": "ru",
    "sec-ch-ua": '"Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "referer": "",
}
OFFLINE_COMBINED = os.path.join(BASE_DIR, "bk_all_menus_combined.json")

POTATO_WORDS = {"фри", "картофель"}

UPSELL_MATRIX = {
    "small_fries":  {"nuggets_3": 4.99, "nuggets_6": 49.99, "nuggets_9": 79.99, "nuggets_12": 109.99, "medium_fries": 29.99, "large_fries": 49.99, "maxx_fries": 119.99},
    "medium_fries": {"nuggets_3": 0.0,  "nuggets_6": 19.99, "nuggets_9": 39.99, "nuggets_12": 69.99, "large_fries": 39.99, "maxx_fries": 89.99},
    "large_fries":  {"nuggets_6": 0.0,  "nuggets_9": 19.99, "nuggets_12": 49.99, "maxx_fries": 49.99},
}
DEFAULT_UPCHARGE = 19.99

# Явный запрет купонов, которые приходят в API, но в UI точки недоступны.
# Ключ — код купона (str); значение — множество store_id или {"*"} для всех точек.
# Раскомментируйте строку, если «Всё по 99,99» не действует в Югорске (1002):
COUPON_BLOCKLIST = {
    # "56472": {"1002"},
}


def validate_store_id(rid):
    rid_str = str(rid).strip()
    if not rid_str.isdigit():
        raise ValueError(f"Invalid restaurant_id: '{rid}' — expected numeric ID (e.g. 1002)")
    return rid_str


def fetch_menu(restaurant_id):
    headers = SESSION_HEADERS.copy()
    headers["X-Store-ID"] = str(restaurant_id)
    headers["X-Region-Id"] = str(restaurant_id)

    payload = {
        "params": {
            "restaurant_id": int(restaurant_id),
            "type": "stay",
            "source": "web",
            "keys": None,
        }
    }

    resp = requests.post(
        "https://orderapp.burgerkingrus.ru/gateway/menu-composition/api/v7/menu",
        headers=headers,
        json=payload,
        timeout=15,
    )
    if not resp.ok:
        print(f"[ERROR] API {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    data = resp.json()

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"menu_{restaurant_id}.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    return data


def load_data(rid="1002", mode="auto"):
    rid = validate_store_id(rid)
    menu = None

    if mode in ("live", "auto"):
        try:
            raw = fetch_menu(rid)
            menu = raw.get("menu", {}).get("result", raw.get("result"))
        except Exception as e:
            if mode == "live":
                raise
            print(f"[WARN] Live request failed ({e}), using offline cache")

    if menu is None:
        dynamic_cache = os.path.join(CACHE_DIR, f"menu_{rid}.json")
        if os.path.exists(dynamic_cache):
            with open(dynamic_cache, encoding="utf-8") as f:
                raw = json.load(f)
            menu = raw.get("menu", {}).get("result") or raw.get("result") or raw
        elif os.path.exists(OFFLINE_COMBINED):
            with open(OFFLINE_COMBINED, encoding="utf-8") as f:
                all_menus = json.load(f)
            if rid in all_menus:
                menu = all_menus[rid].get("menu", {}).get("result") or all_menus[rid].get("result")
        if menu is None:
            raise FileNotFoundError(
                f"No menu data for restaurant {rid}. "
                "Run with mode='live' to download, or provide offline cache."
            )

    with open(STRUCT_PATH, encoding="utf-8") as f:
        struct = json.load(f)

    return menu, struct, rid


def _field(raw, *names, default=None):
    """Ищет первое непустое значение среди имён сначала на верхнем уровне, затем в main_info."""
    for n in names:
        if n in raw and raw[n] not in (None, ""):
            return raw[n]
    mi = raw.get("main_info", {})
    for n in names:
        if n in mi and mi[n] not in (None, ""):
            return mi[n]
    return default


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:
            value = value / 1000
        try:
            return datetime.fromtimestamp(value)
        except (ValueError, OSError):
            return None
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def default_coupon_context(restaurant_id, now=None):
    platform = SESSION_HEADERS.get("x-burgerking-platform", "web_mobile")
    channel_map = {"web_mobile": "web_mobile", "web": "web", "ios": "app", "android": "app"}
    return {
        "store_id": str(restaurant_id),
        "channel": channel_map.get(platform, platform),
        "order_types": {"stay", "in_restaurant", "dine_in", "all", "*"},
        "now": now or datetime.now(),
    }


def validate_coupon_visibility(raw_coupon, ctx=None):
    """Отсекает фантомные/секретные/архивные акции, которых нет в UI меню.
    Возвращает (ok: bool, reason: str)."""
    if ctx is None:
        ctx = default_coupon_context(1002)
    now = ctx["now"]

    # 0. Явный блоклист по точке (надёжный способ отключить неприменимый купон)
    bl = COUPON_BLOCKLIST.get(str(_field(raw_coupon, "code")), set())
    if bl and (ctx["store_id"] in bl or "*" in bl):
        return False, f"blocklisted:{ctx['store_id']}"

    # A. Явные флаги скрытия / секретности
    for f in ("hidden", "is_hidden", "secret", "is_secret", "internal", "is_internal"):
        if _field(raw_coupon, f) is True:
            return False, f"hidden_flag:{f}"

    # B. Флаги видимости и активности
    for f, expect in (("visible", True), ("is_visible", True), ("show_in_menu", True),
                      ("display", True), ("in_menu", True), ("active", True), ("is_active", True)):
        v = _field(raw_coupon, f)
        if v is not None and v != expect:
            return False, f"visibility_flag:{f}={v}"

    # C. Статус / тип промо
    status = str(_field(raw_coupon, "status", "promo_status", "") or "").lower()
    if status in {"archived", "draft", "paused", "disabled", "inactive", "expired"}:
        return False, f"status:{status}"
    promo_type = str(_field(raw_coupon, "promo_type", "type", "coupon_type", "") or "").lower()
    if promo_type in {"secret", "hidden", "archived", "draft", "internal", "push", "crm"}:
        return False, f"promo_type:{promo_type}"

    # D. Каналы продаж / источники
    channels = _field(raw_coupon, "channels", "sales_channels", "available_channels", "sources", "source")
    if channels:
        if isinstance(channels, str):
            channels = [channels]
        norm = {str(c).lower() for c in channels}
        if not (norm & {"all", "*", ctx["channel"], "menu", "web_mobile"}):
            return False, f"channel_mismatch:{sorted(norm)}"

    # D2. Тип заказа (в ресторане vs доставка)
    order_types = _field(raw_coupon, "order_types", "sales_types", "applicable_types", "available_types")
    if order_types:
        if isinstance(order_types, str):
            order_types = [order_types]
        norm = {str(t).lower() for t in order_types}
        if not (norm & ctx["order_types"]):
            return False, f"order_type_mismatch:{sorted(norm)}"

    # E. Привязка к ресторану
    stores = _field(raw_coupon, "restaurant_ids", "store_ids", "restrict_to_stores", "available_stores")
    if stores:
        if isinstance(stores, (int, str)):
            stores = [stores]
        store_set = {str(s) for s in stores}
        if ctx["store_id"] not in store_set and "*" not in store_set:
            return False, f"store_mismatch:{ctx['store_id']}"

    # F. Валидность по датам
    start = _parse_dt(_field(raw_coupon, "date_start", "valid_from", "start_date", "available_from"))
    end = _parse_dt(_field(raw_coupon, "date_end", "valid_to", "end_date", "available_to", "valid_till"))
    notification = raw_coupon.get("notification", {})
    if not end:
        end = _parse_dt(notification.get("date_till"))
    if start and now < start:
        return False, f"not_yet_active"
    if end and now > end:
        return False, f"expired"

    return True, "ok"


def compute_lifecycle(raw_item, restaurant_id, ctx=None):
    mi = raw_item.get("main_info", {})
    restricted = mi.get("restricted", False)
    price = mi.get("price", 0)
    is_coupon = "code" in raw_item
    is_combo = "slots" in raw_item or mi.get("type") == "combo"
    is_visible = True
    reject_reason = None
    if is_coupon:
        cctx = ctx or default_coupon_context(restaurant_id)
        ok, reject_reason = validate_coupon_visibility(raw_item, cctx)
        is_visible = ok

    now = datetime.now()
    date_start = _parse_dt(_field(raw_item, "date_start", "valid_from", "start_date", "available_from", "active_from"))
    date_end = _parse_dt(_field(raw_item, "date_end", "valid_to", "end_date", "available_to", "active_until", "active_to"))
    date_valid = True
    if date_start and now < date_start:
        date_valid = False
    if date_end and now > date_end:
        date_valid = False

    is_active = not restricted and date_valid
    is_available = ((price > 0) or is_combo) and date_valid
    is_visible = is_visible and date_valid

    return {
        "is_active": is_active,
        "is_visible": is_visible,
        "is_available": is_available,
        "is_archived": False,
        "reject_reason": reject_reason if reject_reason else (None if date_valid else "date_expired"),
        "fetched_at": datetime.now().isoformat(),
    }


def is_valid(lc):
    return lc["is_active"] and lc["is_visible"] and lc["is_available"] and not lc["is_archived"]


def build_menu_id_set(menu):
    """Строит множество ID всех комбо и купонов, физически присутствующих в меню ресторана."""
    ids = set()
    for entry in menu.get("combos", []):
        mi = entry.get("main_info", {})
        if "id" in mi:
            ids.add(mi["id"])
    for entry in menu.get("general_coupons", []):
        mi = entry.get("main_info", {})
        if "id" in mi:
            ids.add(mi["id"])
        code = entry.get("code")
        if code:
            ids.add(code)
    return ids


def build_menu_index(menu, restaurant_id="1002"):
    idx = {}
    menu_by_id = {}
    removed = 0
    reasons = {"price_zero": 0, "restricted": 0}

    for d in menu["dishes"]:
        mi = d["main_info"]
        p, name, did = mi["price"], mi["name"].strip(), mi["id"]
        lc = compute_lifecycle(d, restaurant_id)

        if not is_valid(lc):
            removed += 1
            if p == 0:
                reasons["price_zero"] += 1
            if mi.get("restricted"):
                reasons["restricted"] += 1
            continue

        idx[name.lower()] = {
            "name": name, "price": p / 100, "id": did,
            "combo_only": False, "ids": {did},
            "lifecycle": lc,
        }
        menu_by_id[did] = idx[name.lower()]

    return idx, menu_by_id, removed, reasons


def build_combo_prices(menu, restaurant_id="1002"):
    prices = {}
    skipped = 0
    coupon_reasons = {}
    for entry in menu.get("combos", []):
        lc = compute_lifecycle(entry, restaurant_id)
        if not is_valid(lc):
            skipped += 1
            continue
        mi = entry["main_info"]
        prices[mi["id"]] = mi["price"] / 100
    for entry in menu.get("general_coupons", []):
        lc = compute_lifecycle(entry, restaurant_id)
        if not is_valid(lc):
            skipped += 1
            reason = (lc.get("reject_reason") or "unknown").split(":")[0]
            coupon_reasons[reason] = coupon_reasons.get(reason, 0) + 1
            name = entry.get("main_info", {}).get("name", "?")
            print(f"  [COUPON SKIP] {name!r}: {lc.get('reject_reason')}")
            continue
        mi = entry["main_info"]
        prices[mi["id"]] = mi["price"] / 100
    if coupon_reasons:
        print(f"  [COUPON FILTER] причины отбраковки: {coupon_reasons}")
    return prices, skipped


def tag_side(name):
    nl = name.lower()
    if "наггетсы (3 шт.)" in nl:
        return "nuggets_3"
    if "наггетсы (6 шт.)" in nl:
        return "nuggets_6"
    if "наггетсы (9 шт.)" in nl:
        return "nuggets_9"
    if "наггетсы (12 шт.)" in nl:
        return "nuggets_12"
    if "maxx" in nl and ("фри" in nl or "картофель" in nl):
        return "maxx_fries"
    if ("большой" in nl or "стандартный" in nl) and ("фри" in nl or "картофель" in nl):
        return "large_fries" if "большой" in nl else "medium_fries"
    if "малый" in nl and ("фри" in nl or "картофель" in nl):
        return "small_fries"
    return None


def tag_sauce(name):
    nl = name.lower()
    if "соус" in nl or "кетчуп" in nl:
        return "sauce"
    return None


def normalize_combo(raw_combo, struct_entry, restaurant_id, menu_by_id=None):
    mi = raw_combo["main_info"]
    cid = mi["id"]
    base_price = mi["price"]

    required_modifier_groups = []
    calculated_base_price = 0

    for si, slot in enumerate(struct_entry.get("slots", [])):
        raw_dishes = slot.get("dishes", [])
        options = []
        for d in raw_dishes:
            dish_name = (d.get("menu_name") or d.get("name") or "").strip()
            if not dish_name:
                continue
            did = d.get("dish_id")
            price_kopecks = d.get("price", 0)
            if menu_by_id is not None and did and did not in menu_by_id:
                continue
            if menu_by_id is not None and did and did in menu_by_id:
                if menu_by_id[did].get("price", 0) == 0:
                    continue
            options.append({
                "option_id": str(did) if did else dish_name,
                "name": dish_name,
                "price_delta_kopecks": 0,
                "is_default": False,
                "tag": tag_side(dish_name),
                "_raw_price": price_kopecks,
            })

        if not options:
            continue

        cheapest_opt = min(options, key=lambda o: o["_raw_price"])
        calculated_base_price += cheapest_opt["_raw_price"]

        for o in options:
            o["price_delta_kopecks"] = o["_raw_price"] - cheapest_opt["_raw_price"]

        sauce_count = sum(1 for o in options if tag_sauce(o["name"]))
        is_sauce_slot = sauce_count >= 2 and sauce_count == len(options)

        if not is_sauce_slot:
            cheapest_opt["is_default"] = True
        else:
            options[0]["is_default"] = True

        for o in options:
            o.pop("_raw_price")

        required_modifier_groups.append({
            "group_id": str(slot.get("slot_id", si)),
            "name": slot.get("slot_name"),
            "is_required": True,
            "min_select": 1,
            "max_select": 1,
            "is_sauce_slot": is_sauce_slot,
            "options": options,
        })

    if base_price > 0:
        final_base_price = base_price
    elif calculated_base_price > 0:
        final_base_price = calculated_base_price
    else:
        final_base_price = 0

    return {
        "id": str(cid),
        "code": cid,
        "name": mi["name"].strip(),
        "restaurant_id": int(restaurant_id),
        "type": "combo",
        "lifecycle": compute_lifecycle(raw_combo, restaurant_id),
        "pricing": {
            "base_price_kopecks": final_base_price,
            "raw_base_price_kopecks": base_price,
            "calculated_base_price_kopecks": calculated_base_price,
            "currency": "RUB",
        },
        "required_modifier_groups": required_modifier_groups,
    }


def normalize_coupon(raw_coupon, struct_entry, restaurant_id):
    mi = raw_coupon["main_info"]
    cid = mi["id"]
    code = raw_coupon.get("code", "")

    required_modifier_groups = []
    if struct_entry:
        for si, slot in enumerate(struct_entry.get("slots", [])):
            raw_dishes = slot.get("dishes", [])
            options = []
            for d in raw_dishes:
                dish_name = (d.get("menu_name") or d.get("name") or "").strip()
                if not dish_name:
                    continue
                did = d.get("dish_id")
                price_kopecks = d.get("price", 0)
                options.append({
                    "option_id": str(did) if did else dish_name,
                    "name": dish_name,
                    "price_delta_kopecks": 0,
                    "is_default": False,
                    "tag": tag_side(dish_name),
                    "_raw_price": price_kopecks,
                })

            if not options:
                continue

            min_price = min(o["_raw_price"] for o in options)
            for o in options:
                o["price_delta_kopecks"] = o["_raw_price"] - min_price

            sauce_count = sum(1 for o in options if tag_sauce(o["name"]))
            is_sauce_slot = sauce_count >= 2 and sauce_count == len(options)

            if not is_sauce_slot:
                cheapest_idx = min(range(len(options)), key=lambda i: options[i]["_raw_price"])
                options[cheapest_idx]["is_default"] = True
            else:
                options[0]["is_default"] = True

            for o in options:
                o.pop("_raw_price")

            required_modifier_groups.append({
                "group_id": str(slot.get("slot_id", si)),
                "name": slot.get("slot_name"),
                "is_required": True,
                "min_select": 1,
                "max_select": 1,
                "is_sauce_slot": is_sauce_slot,
                "options": options,
            })

    return {
        "id": str(cid),
        "code": code,
        "name": mi["name"].strip(),
        "restaurant_id": int(restaurant_id),
        "type": "product",
        "lifecycle": compute_lifecycle(raw_coupon, restaurant_id),
        "pricing": {
            "base_price_kopecks": mi["price"],
            "currency": "RUB",
        },
        "required_modifier_groups": required_modifier_groups if required_modifier_groups else None,
    }


def _side_family(tag):
    """Семейство гарнира: 'fries' для картошки, 'nuggets' для наггетсов, иначе None."""
    if not tag:
        return None
    if tag.endswith("_fries"):
        return "fries"
    if tag.endswith("_nuggets"):
        return "nuggets"
    return None


def compute_effective_combo_price(base_price_kopecks, modifier_groups, chosen_indices, side_slot_index=None, upsell_matrix=None, default_upcharge=None):
    if upsell_matrix is None:
        upsell_matrix = UPSELL_MATRIX
    if default_upcharge is None:
        default_upcharge = DEFAULT_UPCHARGE

    total_delta = 0
    sauce_delta = 0
    upsell_delta = 0

    for gi, group in enumerate(modifier_groups):
        if gi not in chosen_indices:
            continue

        chosen_opt = group["options"][chosen_indices[gi]]
        delta = chosen_opt["price_delta_kopecks"]

        if group.get("is_sauce_slot"):
            sauce_delta += delta
        else:
            total_delta += delta

            if side_slot_index is not None and gi == side_slot_index:
                default_opt = None
                for o in group["options"]:
                    if o["is_default"]:
                        default_opt = o
                        break
                if default_opt and chosen_opt["name"] != default_opt["name"]:
                    def_tag = default_opt.get("tag")
                    chosen_tag = chosen_opt.get("tag")
                    # UPSELL только при смене ВИДА гарнира (картошка <-> наггетсы),
                    # НЕ при смене размера внутри одного вида (малый->стандарт->большой->maxx).
                    def_fam = _side_family(def_tag)
                    chosen_fam = _side_family(chosen_tag)
                    if def_fam and chosen_fam and def_fam != chosen_fam:
                        upsell_delta += upsell_matrix.get(def_tag, {}).get(chosen_tag, default_upcharge) * 100

    return base_price_kopecks + total_delta + sauce_delta + upsell_delta
